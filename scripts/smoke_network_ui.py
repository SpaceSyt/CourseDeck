"""LAN UI smoke with synthetic API responses and a loopback-only static server."""

import asyncio
import mimetypes
import tempfile
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from coursedeck.app import create_app
from scripts.smoke_mail_rules_ui import QuietHandler


async def check(base_url, app, output):
    config = {
        "enabled": False,
        "host": "",
        "port": 48322,
        "certfile": "",
        "keyfile": "",
        "password_set": False,
        "active": False,
        "active_url": None,
        "restart_required": False,
        "error": None,
    }
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    writes = []
    authenticated = False
    signed_in_once = False
    protected_reads = []
    login_calls = []
    save_started, release_save = asyncio.Event(), asyncio.Event()
    delay_save = False
    recovery_mode = False

    async def route_api(route):
        nonlocal authenticated, signed_in_once
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path
        remote = parsed.hostname == "coursedeck.test"
        if path == "/api/network/session":
            await route.fulfill(json={"local": not remote, "authenticated": authenticated})
            return
        if path == "/api/network/login":
            assert request.headers.get("x-coursedeck") == "1"
            login_calls.append(True)
            authenticated = request.post_data_json["password"] == "synthetic-password-only"
            signed_in_once = signed_in_once or authenticated
            await route.fulfill(
                status=200 if authenticated else 401,
                json={"ok": True} if authenticated else {"detail": "Invalid LAN password."},
            )
            return
        if path == "/api/network/logout":
            authenticated = False
            await route.fulfill(json={"ok": True})
            return
        if remote:
            protected_reads.append(path)
            if not authenticated:
                assert signed_in_once, "Workspace requested before remote authentication"
                # Requests already in flight when access is revoked must be denied.
                await route.fulfill(status=401, json={"detail": "LAN sign-in required."})
                return
            assert not path.startswith("/api/recovery/") or path == "/api/recovery/status", (
                "Remote recovery must not fetch administration controls"
            )
        if recovery_mode and path == "/api/snapshot":
            await route.fulfill(status=503, json={"detail": "Recovery required."})
            return
        if recovery_mode and path == "/api/recovery/status":
            await route.fulfill(json={"maintenance": False, "recovery_required": True})
            return
        if path == "/api/network":
            assert not remote, "Remote settings must not fetch local administration"
            if request.method == "PUT":
                payload = request.post_data_json
                # Never retain the synthetic password in request histories or output.
                writes.append({key: value for key, value in payload.items() if key != "password"})
                if delay_save:
                    save_started.set()
                    await release_save.wait()
                if payload["enabled"] and payload["host"] == "8.8.8.8":
                    await route.fulfill(status=422, json={"detail": "Use a private IPv4 address."})
                    return
                config.update({key: value for key, value in payload.items() if key != "password"})
                config["error"] = None
                config["password_set"] = config["password_set"] or bool(payload.get("password"))
                config["restart_required"] = config["enabled"]
                if not config["enabled"]:
                    config["active"] = False
                    config["active_url"] = None
            await route.fulfill(json=config)
            return
        if path.startswith("/api/desktop/"):
            assert request.method == "GET"
            await route.fulfill(json={"supported": False, "enabled": False})
            return
        response = client.request(
            request.method,
            path + ("?" + parsed.query if parsed.query else ""),
            headers={"X-CourseDeck": "1", "Content-Type": "application/json"},
            content=request.post_data,
        )
        await route.fulfill(
            status=response.status_code, content_type="application/json", body=response.text
        )

    async def route_remote(route):
        path = urlsplit(route.request.url).path
        if path.startswith("/api/"):
            await route_api(route)
            return
        root = Path("frontend/dist").resolve()
        file = (root / path.lstrip("/")).resolve() if path != "/" else root / "index.html"
        assert file.is_relative_to(root)
        await route.fulfill(
            body=file.read_bytes(),
            content_type={".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(
                file.suffix, mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            ),
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        errors = []
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/api/**", route_api)
            await page.goto(base_url)
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            panel = page.get_by_role("region", name="LAN access", exact=True)
            toggle = panel.get_by_label("Enable LAN access after restart")
            await expect(toggle).not_to_be_checked()
            await expect(panel).to_contain_text("Local access only")
            await expect(panel.get_by_role("button", name="Save LAN access")).to_be_disabled()
            await toggle.check()
            await panel.get_by_label("LAN IPv4 address").fill("8.8.8.8")
            await panel.get_by_label("TLS certificate path").fill("C:\\synthetic\\certificate.pem")
            await panel.get_by_label("TLS private key path").fill("C:\\synthetic\\key.pem")
            await panel.get_by_label("LAN password").fill("synthetic-password-only")
            await panel.get_by_role("button", name="Save LAN access").click()
            await expect(panel.get_by_role("alert")).to_have_text("Use a private IPv4 address.")
            assert not config["enabled"]
            await expect(panel.get_by_label("LAN password")).to_have_value("")
            await panel.get_by_label("LAN IPv4 address").fill("192.168.50.20")
            await panel.get_by_label("LAN password").fill("synthetic-password-only")
            delay_save = True
            await panel.get_by_role("button", name="Save LAN access").click()
            await asyncio.wait_for(save_started.wait(), 5)
            await expect(toggle).to_be_disabled()
            await panel.get_by_role("button", name="Save LAN access").evaluate("el => el.click()")
            assert len(writes) == 2
            release_save.set()
            await expect(panel).to_contain_text("Restart CourseDeck to apply LAN access")
            await expect(panel.get_by_label("LAN password")).to_have_value("")
            await page.reload()
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(toggle).to_be_checked()
            await expect(panel).to_contain_text("https://192.168.50.20:48322")
            await expect(panel).to_contain_text("Local access only")
            await panel.screenshot(path=str(output / "network-settings-desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await panel.screenshot(path=str(output / "network-settings-mobile.png"))
            config.update(
                active=True, active_url="https://192.168.50.20:48322", restart_required=False
            )
            await page.reload()
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(panel).to_contain_text("Active: https://192.168.50.20:48322")
            await panel.get_by_label("HTTPS port").fill("")
            await panel.get_by_role("button", name="Disable LAN access").click()
            await expect(toggle).not_to_be_checked()
            await expect(panel).to_contain_text("Local access only")
            assert not config["active"] and not config["restart_required"]
            assert writes[-1] == {"enabled": False}
            await page.reload()
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(toggle).not_to_be_checked()

            config.update(
                host="",
                certfile="",
                keyfile="",
                password_set=False,
                error="LAN configuration is invalid.",
            )
            await page.reload()
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(panel.get_by_role("alert")).to_have_text("LAN configuration is invalid.")
            await expect(toggle).not_to_be_checked()
            await panel.get_by_role("button", name="Save LAN access").click()
            await expect(panel.get_by_role("alert")).to_have_count(0)
            assert writes[-1] == {"enabled": False}
            assert config["error"] is None

            recovery_mode = True
            config.update(enabled=True, active=True, active_url="https://192.168.50.20:48322")
            await page.reload()
            await expect(panel).to_be_visible()
            await panel.get_by_role("button", name="Disable LAN access").click()
            await expect(toggle).not_to_be_checked()
            assert not config["active"] and not config["enabled"]
            recovery_mode = False

            remote = await browser.new_page(viewport={"width": 390, "height": 844})
            remote.on("pageerror", lambda error: errors.append(str(error)))
            # All fake-host requests are fulfilled in memory; no LAN listener or request.
            await remote.route("**/*", route_remote)
            await remote.goto("https://coursedeck.test/")
            await expect(remote.get_by_role("heading", name="LAN sign-in")).to_be_visible()
            assert protected_reads == []
            await remote.get_by_label("LAN password").fill("wrong-password")
            await remote.get_by_role("button", name="Sign in", exact=True).click()
            await expect(remote.get_by_role("alert")).to_have_text("Invalid LAN password.")
            assert protected_reads == []
            await remote.screenshot(path=str(output / "network-login-mobile.png"))
            await remote.get_by_label("LAN password").fill("synthetic-password-only")
            await remote.get_by_role("button", name="Sign in", exact=True).click()
            await expect(
                remote.locator("nav").get_by_role("button", name="Todo", exact=False)
            ).to_be_visible()
            await remote.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(
                remote.get_by_text("Manage settings on the computer running CourseDeck.")
            ).to_be_visible()
            await remote.locator("nav").get_by_role("button", name="Sources", exact=True).click()
            await expect(
                remote.get_by_text("Manage source connections on the computer running CourseDeck.")
            ).to_be_visible()
            assert protected_reads
            recovery_mode = True
            await remote.reload()
            await expect(
                remote.get_by_text("Recover this workspace on the computer running CourseDeck.")
            ).to_be_visible()
            await expect(remote.get_by_role("region", name="LAN access", exact=True)).to_have_count(
                0
            )
            recovery_mode = False
            await remote.reload()
            await expect(remote.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
            authenticated = False
            await remote.evaluate("window.dispatchEvent(new Event('focus'))")
            await expect(remote.get_by_role("heading", name="LAN sign-in")).to_be_visible()
            await expect(remote.locator("nav")).to_have_count(0)
            await remote.get_by_label("LAN password").fill("synthetic-password-only")
            await remote.get_by_role("button", name="Sign in", exact=True).click()
            await remote.get_by_role("button", name="Sign out", exact=True).click()
            await expect(remote.get_by_role("heading", name="LAN sign-in")).to_be_visible()
            assert not authenticated
            assert len(login_calls) == 3
            assert await remote.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
        finally:
            release_save.set()
            await browser.close()
            client.close()


def main():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(Path("frontend/dist").resolve()))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path(tempfile.mkdtemp(prefix="coursedeck-network-ui-evidence-"))
    try:
        with tempfile.TemporaryDirectory(prefix="coursedeck-network-ui-data-") as directory:
            app = create_app(Path(directory))
            try:
                asyncio.run(check(f"http://127.0.0.1:{server.server_port}", app, output))
            finally:
                app.state.close_readers()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("LAN UI passed: off by default, invalid save, save guard, saved/active state, disable,")
    print("reload, login gating, rejected password, local-only admin, revocation, logout, mobile.")
    print("Corrupt LAN config repair and local/remote database recovery controls passed.")
    print(f"Synthetic UI evidence: {output}")


if __name__ == "__main__":
    main()
