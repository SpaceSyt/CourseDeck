"""Optional HTTPS listener sharing the local application's single lifespan."""

import asyncio
import ipaddress
import ssl
from contextlib import ExitStack, asynccontextmanager, contextmanager

import uvicorn

from .runtime import listening_socket

_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_START_TIMEOUT = 5
_STOP_TIMEOUT = 15


class _SecondaryServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Process signals belong to the primary loopback server.
        yield


class LanServer:
    def __init__(self, app):
        self.app = app
        self.access = app.state.network
        self.server = None
        self.task = None
        self.resources = ExitStack()

    async def start(self):
        try:
            settings = self.access.starting()
            if settings is None:
                return
            address = ipaddress.IPv4Address(settings.host)
            if not any(address in network for network in _PRIVATE_NETWORKS):
                raise ValueError("A private IPv4 interface is required")
            config = uvicorn.Config(
                self.app,
                host=settings.host,
                port=settings.port,
                lifespan="off",
                proxy_headers=False,
                access_log=False,
                log_config=None,
                ssl_certfile=settings.certfile,
                ssl_keyfile=settings.keyfile,
                ssl_version=ssl.PROTOCOL_TLS_SERVER,
                timeout_graceful_shutdown=10,
            )
            # Validate and load TLS before reserving any externally reachable socket.
            config.load()
            if config.ssl is None:
                raise ValueError("HTTPS is required")
            config.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
            sock = self.resources.enter_context(listening_socket(settings.port, host=settings.host))
            self.server = _SecondaryServer(config)
            self.task = asyncio.create_task(self._serve(sock), name="CourseDeck LAN")
            async with asyncio.timeout(_START_TIMEOUT):
                while not self.server.started and not self.task.done():
                    await asyncio.sleep(0.01)
            if self.server.started and not self.task.done():
                self.access.mark_started(settings.host, settings.port)
        except (Exception, SystemExit) as exc:
            self.access.mark_stopped(
                f"LAN listener could not start ({type(exc).__name__}). "
                "Check the address, port and HTTPS files."
            )
            await self.stop()

    async def _serve(self, sock):
        try:
            await self.server.serve(sockets=[sock])
            if not self.server.should_exit:
                self.access.mark_stopped("LAN listener stopped unexpectedly. Restart CourseDeck.")
        except (Exception, SystemExit) as exc:
            self.access.mark_stopped(
                f"LAN listener stopped ({type(exc).__name__}). Restart CourseDeck."
            )
        finally:
            self.access.mark_stopped(self.access.error)
            self.resources.close()

    async def stop(self):
        self.access.mark_stopped(self.access.error)
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=_STOP_TIMEOUT)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        self.resources.close()


@asynccontextmanager
async def lan_listener(app):
    listener = LanServer(app)
    app.state.lan_server = listener
    try:
        await listener.start()
        yield
    finally:
        await listener.stop()
