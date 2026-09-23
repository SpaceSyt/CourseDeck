"""Windows tray entry owning one local service and its graceful shutdown."""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser

import httpx
import uvicorn

from .runtime import StartupError, data_directory, instance_lock, listening_socket


def show_error(message):
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "CourseDeck", 0x10)
    elif sys.stderr:
        print(message, file=sys.stderr)


def existing_tray_url(directory):
    """A stale descriptor or a different service must never count as our instance."""
    try:
        record = json.loads((directory / "tray.json").read_text(encoding="utf-8"))
        port = record["port"]
        identity = record["instance"]
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            return None
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=2) as client:
            response = client.get(url + "/api/tray")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("instance") == identity:
                return url
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        pass
    return None


class TrayService:
    def __init__(self, directory, sock, *, app_factory=None):
        self.directory = directory
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.instance = secrets.token_hex(16)
        self.app_factory = app_factory
        self.server = None
        self.failure = None
        self.loop = None
        self.serve_task = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="CourseDeck service")

    def _run(self):
        try:
            factory = self.app_factory
            if factory is None:
                from .app import create_app

                def factory():
                    return create_app(self.directory)

            def application():
                app = factory()

                @app.get("/api/tray")
                async def identity():
                    return {"instance": self.instance}

                # The web UI has a catch-all route; identity must precede it.
                app.router.routes.insert(0, app.router.routes.pop())
                return app

            self.server = uvicorn.Server(
                uvicorn.Config(
                    application,
                    factory=True,
                    host="127.0.0.1",
                    port=self.port,
                    access_log=False,
                    log_config=None,
                    timeout_graceful_shutdown=10,
                )
            )

            async def serve():
                self.loop = asyncio.get_running_loop()
                self.serve_task = asyncio.current_task()
                if not self.stopping.is_set():
                    await self.server.serve(sockets=[self.sock])

            asyncio.run(serve())
        except asyncio.CancelledError:
            if not self.stopping.is_set():
                self.failure = "CancelledError"
        except BaseException as exc:
            # Exception text may contain school URLs or authentication details.
            self.failure = type(exc).__name__

    @property
    def ready(self):
        return bool(
            self.server
            and self.server.started
            and self.thread.is_alive()
            and not self.stopping.is_set()
        )

    def start(self, timeout=45):
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.ready:
            if not self.thread.is_alive():
                raise StartupError("The local service could not start.")
            if time.monotonic() >= deadline:
                raise StartupError("The local service took too long to start.")
            time.sleep(0.05)
        temporary = self.directory / "tray.json.tmp"
        temporary.write_text(
            json.dumps({"port": self.port, "instance": self.instance}), encoding="utf-8"
        )
        temporary.replace(self.directory / "tray.json")

    def stop(self):
        self.stopping.set()

        def request_stop():
            if self.server is not None:
                self.server.should_exit = True
                # Uvicorn waits for lifespan startup before checking should_exit.
                # Cancelling here lets asyncio run its lifespan cleanup if startup stalls.
                if not self.server.started and self.serve_task is not None:
                    self.serve_task.cancel()

        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(request_stop)
            except RuntimeError:
                pass  # The service finished between the loop check and the request.
        if self.thread.ident is not None:
            self.thread.join()
        # Keep the data lock until ASGI cleanup has closed sync jobs and browsers.
        for name in ("tray.json", "tray.json.tmp"):
            (self.directory / name).unlink(missing_ok=True)

    def sync(self):
        if not self.ready:
            raise StartupError("The local service is disconnected.")
        with httpx.Client(base_url=self.url, trust_env=False, timeout=5) as client:
            client.post("/api/sync", headers={"X-CourseDeck": "1"}).raise_for_status()
            mail = client.get("/api/mail", params={"limit": 1})
            mail.raise_for_status()
            if mail.json().get("connection", {}).get("status") == "connected":
                client.post(
                    "/api/mail/connection/sync", headers={"X-CourseDeck": "1"}
                ).raise_for_status()


def tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=15, fill="#17242a")
    draw.rounded_rectangle((17, 13, 48, 47), radius=4, outline="#789888", width=3)
    draw.rounded_rectangle((12, 20, 43, 53), radius=4, fill="#17242a", outline="#b3d6c2", width=3)
    draw.line((20, 34, 27, 41, 36, 29), fill="#b3d6c2", width=4)
    return image


def run_tray(service, *, open_browser=True):
    import pystray

    sync_lock = threading.Lock()
    quitting = threading.Event()

    def open_app(icon=None, item=None):
        webbrowser.open(service.url)

    def sync_now(icon, item):
        if not sync_lock.acquire(blocking=False):
            return

        def work():
            try:
                service.sync()
            except (StartupError, httpx.HTTPError, ValueError):
                icon.notify("Sync could not start. Open CourseDeck to check the connection.")
            finally:
                sync_lock.release()

        threading.Thread(target=work, name="CourseDeck sync", daemon=True).start()

    def quit_app(icon, item):
        if quitting.is_set():
            return
        quitting.set()
        icon.title = "CourseDeck — Stopping"
        icon.update_menu()

        def finish():
            service.stop()
            icon.stop()

        threading.Thread(target=finish, name="CourseDeck shutdown").start()

    def setup(icon):
        icon.visible = True
        if open_browser:
            open_app()
        while not quitting.wait(1):
            if not service.ready:
                icon.title = "CourseDeck — Disconnected"
                icon.update_menu()
                icon.notify("Local service stopped. Exit and reopen CourseDeck.")
                return

    icon = pystray.Icon(
        "CourseDeck",
        tray_image(),
        "CourseDeck",
        pystray.Menu(
            pystray.MenuItem("Open CourseDeck", open_app, default=True),
            pystray.MenuItem(
                "Sync now", sync_now, enabled=lambda item: service.ready and not quitting.is_set()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", quit_app, enabled=lambda item: not quitting.is_set()),
        ),
    )
    try:
        icon.run(setup=setup)
    finally:
        quitting.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run CourseDeck in the Windows system tray")
    parser.add_argument("--port", type=int, default=48321)
    parser.add_argument("--data-dir")
    parser.add_argument("--background", action="store_true", help="Start without opening a browser")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("the tray entry currently supports Windows only")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # pythonw has no standard streams; libraries still expect usable file handles.
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w"))
    directory = data_directory(args.data_dir)
    try:
        with instance_lock(directory):
            with listening_socket(args.port) as sock:
                service = TrayService(directory, sock)
                try:
                    service.start()
                    run_tray(service, open_browser=not args.background)
                finally:
                    service.stop()
    except (StartupError, OSError) as exc:
        url = existing_tray_url(directory)
        if url:
            if not args.background:
                webbrowser.open(url)
            return 0
        show_error(f"{exc}\nIf CourseDeck is running in a terminal, close it first.")
        return 1
    except Exception as exc:
        show_error(f"CourseDeck could not start ({type(exc).__name__}). Run setup.cmd and retry.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
