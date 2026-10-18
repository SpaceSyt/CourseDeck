"""Reminder preferences and snooze use synthetic local data and never the OS notifier."""

import asyncio
import tempfile
import threading
from datetime import timedelta
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from coursedeck.app import create_app
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.reminders import ReminderEngine
from scripts.smoke_mail_rules_ui import QuietHandler


async def check(base_url, app):
    db = app.state.db
    db.apply(
        "fixture",
        SyncResult(
            outcome=Outcome.SUCCESS,
            courses=[Course(provider="fixture", external_id="c", name="Fixture course")],
            tasks=[
                Task(
                    provider="fixture",
                    external_id="t",
                    course_external_id="c",
                    title="Fixture deadline",
                    due_at=now() + timedelta(hours=1),
                )
            ],
        ),
        now().isoformat(),
    )
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    settings_writes = []
    snoozes = []
    save_started, release_save = asyncio.Event(), asyncio.Event()
    delay_save = False

    async def route_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        if parsed.path.startswith("/api/desktop/"):
            assert request.method == "GET", "Reminder settings must not alter the OS"
            await route.fulfill(json={"supported": False, "enabled": False})
            return
        if parsed.path == "/api/settings" and request.method == "PUT":
            settings_writes.append(request.post_data_json)
            if delay_save:
                save_started.set()
                await release_save.wait()
        if parsed.path == "/api/reminders/snooze":
            snoozes.append(True)
        response = client.request(
            request.method,
            path,
            headers={"X-CourseDeck": "1", "Content-Type": "application/json"},
            content=request.post_data,
        )
        await route.fulfill(
            status=response.status_code, content_type="application/json", body=response.text
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/api/**", route_api)
            await page.goto(base_url)
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            panel = page.get_by_role("region", name="Deadline reminders", exact=True)
            await expect(panel.get_by_label("Enable deadline reminders")).not_to_be_checked()
            await expect(panel.get_by_label("Quiet hours start")).to_have_value("22:00")
            await expect(panel.get_by_label("Quiet hours end")).to_have_value("08:00")
            await expect(panel.get_by_role("button", name="Snooze 30 minutes")).to_be_disabled()
            await panel.get_by_label("Enable deadline reminders").check()
            await panel.get_by_label("Quiet hours", exact=True).uncheck()
            delay_save = True
            save = panel.get_by_role("button", name="Save reminders", exact=True)
            await save.click()
            await asyncio.wait_for(save_started.wait(), 5)
            await expect(save).to_be_disabled()
            # Bypass Playwright's disabled-element waiting to exercise a rapid repeat.
            await save.evaluate("element => element.click()")
            assert len(settings_writes) == 1
            release_save.set()
            await expect(panel.get_by_label("Enable deadline reminders")).to_be_enabled()
            assert db.settings().reminders.enabled
            assert not db.settings().reminders.quiet_enabled
            assert ReminderEngine(db).claim()["count"] == 1
            await page.reload()
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(panel.get_by_label("Enable deadline reminders")).to_be_checked()
            await panel.get_by_text("Recent reminder (1)", exact=True).click()
            await expect(panel).to_contain_text("Fixture deadline")
            await expect(panel).to_contain_text("Submission status unknown")
            Path("data/debug").mkdir(parents=True, exist_ok=True)
            await panel.screenshot(path="data/debug/reminders-desktop.png")
            await panel.get_by_role("button", name="Snooze 30 minutes").click()
            await expect(panel.get_by_role("status")).to_contain_text("Snoozed until")
            assert len(snoozes) == 1
            until = ReminderEngine(db).status()["snoozed_until"]
            await page.reload()
            await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
            await expect(panel.get_by_role("status")).to_contain_text("Snoozed until")
            assert ReminderEngine(db).status()["snoozed_until"] == until
            await page.set_viewport_size({"width": 390, "height": 844})
            await panel.get_by_label("Quiet hours", exact=True).check()
            await expect(panel.get_by_label("Quiet hours start")).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await panel.screenshot(path="data/debug/reminders-mobile.png")
            await panel.get_by_label("Enable deadline reminders").uncheck()
            await panel.get_by_role("button", name="Save reminders").click()
            await expect(panel.get_by_role("button", name="Snooze 30 minutes")).to_be_disabled()
            assert not db.settings().reminders.enabled
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
    try:
        with tempfile.TemporaryDirectory(prefix="coursedeck-reminders-ui-") as directory:
            app = create_app(Path(directory))
            try:
                asyncio.run(check(f"http://127.0.0.1:{server.server_port}", app))
            finally:
                app.state.close_readers()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(
        "Reminders UI passed: defaults, save guard, persistence, caution, snooze, mobile, disable."
    )


if __name__ == "__main__":
    main()
