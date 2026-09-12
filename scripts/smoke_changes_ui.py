"""Exercise built Changes UI with real endpoints and an isolated temporary database."""

import asyncio
import re
import tempfile
import threading
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from coursedeck.changes import build_changes_router, list_changes, summary
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def check(base_url, directory):
    db = Database(directory / "coursedeck.sqlite3")
    courses = [
        Course(provider="fixture", external_id=key, name=title)
        for key, title in [("math", "Calculus fixture"), ("writing", "Writing fixture")]
    ]
    tasks = [
        Task(
            provider="fixture",
            course_external_id=course.external_id,
            external_id="hw",
            title=course.name + " homework",
            description="Original instructions",
            submission_status="submitted",
            due_at=datetime(2026, 9, 20, 15, tzinfo=UTC),
            url="https://example.test/assignment",
        )
        for course in courses
    ]
    result = SyncResult(outcome=Outcome.SUCCESS, complete=True, courses=courses, tasks=tasks)
    db.apply("fixture", result, now().isoformat())
    tasks[0].due_at = datetime(2026, 9, 18, 15, tzinfo=UTC)
    tasks[0].submission_status = "assigned"
    tasks[0].description = "Revised instructions\n  keep_indentation <script>unsafe()</script>"
    db.apply("fixture", result, now().isoformat())
    app = FastAPI()
    app.include_router(build_changes_router(db))

    @app.get("/api/snapshot")
    async def snapshot():
        return {
            "courses": db.courses(),
            "source_courses": db.source_courses(),
            "tasks": db.tasks(),
            "sources": [],
            "settings": db.settings().model_dump(),
            "changes": summary(db),
            "revision": 1,
        }

    @app.get("/api/heartbeat")
    async def heartbeat():
        return {"service": "coursedeck", "status": "connected"}

    @app.get("/api/mail")
    async def mail():
        return {
            "messages": [],
            "total": 0,
            "has_more": False,
            "connection": {"status": "disconnected", "syncing": False},
        }

    client = TestClient(app)

    async def route_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        response = client.request(
            request.method,
            parsed.path + ("?" + parsed.query if parsed.query else ""),
            content=request.post_data or None,
            headers={"Content-Type": "application/json", "X-CourseDeck": "1"},
        )
        await route.fulfill(
            status=response.status_code, content_type="application/json", body=response.text
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000}, locale="en-US")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/**", route_api)
        await page.goto(base_url)
        await page.locator("nav").get_by_role("button", name=re.compile(r"^Changes")).click()
        await expect(page.locator(".change-entry")).to_have_count(5)
        await page.get_by_label("Filter change type").select_option("deadline_earlier")
        await expect(page.locator(".change-entry")).to_have_count(1)
        await expect(page.locator(".changes-diff")).to_contain_text("Sep 20, 2026")
        await expect(page.locator(".changes-diff")).to_contain_text("Sep 18, 2026")
        await expect(page.locator(".change-priority")).to_have_text("Important")
        await page.get_by_role("button", name="Mark read", exact=True).click()
        await expect(page.get_by_role("button", name="Mark unread", exact=True)).to_be_visible()
        await page.reload()
        await page.locator("nav").get_by_role("button", name=re.compile(r"^Changes")).click()
        await page.get_by_label("Filter change type").select_option("deadline_earlier")
        await expect(page.get_by_role("button", name="Mark unread", exact=True)).to_be_visible()
        count, unread = list_changes(db)["total"], summary(db)["unread_count"]
        db.apply("fixture", result, now().isoformat())
        await page.get_by_role("button", name="Refresh changes", exact=True).click()
        await expect(page.get_by_role("button", name="Mark unread", exact=True)).to_be_visible()
        assert list_changes(db)["total"] == count and summary(db)["unread_count"] == unread
        await page.get_by_label("Filter change type").select_option("body_changed")
        await page.get_by_text("View instruction changes", exact=True).click()
        await expect(page.locator(".changes-diff")).to_contain_text("Original instructions")
        await expect(page.locator(".changes-diff")).to_contain_text("keep_indentation")
        assert await page.locator(".changes-diff script").count() == 0
        output = Path("data/debug")
        output.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(output / "changes-desktop.png"), full_page=True)
        await page.get_by_label("Filter change type").select_option("")
        await page.get_by_label("Search changes", exact=True).fill("keep_indentation")
        await expect(page.locator(".change-entry")).to_have_count(1)
        await page.get_by_label("Search changes", exact=True).fill("no-such-change")
        await expect(page.get_by_text("No changes found.", exact=True)).to_be_visible()
        await page.get_by_label("Search changes", exact=True).fill("")
        await expect(page.locator(".change-entry")).to_have_count(5)
        await page.get_by_label("Filter changes by course").select_option(courses[1].id)
        await expect(page.locator(".change-entry")).to_have_count(1)
        await page.get_by_label("Unread", exact=True).check()
        await expect(page.locator(".change-entry")).to_have_count(1)
        await page.get_by_role("button", name="Mark shown read", exact=True).click()
        await expect(page.get_by_text("No changes found.", exact=True)).to_be_visible()
        await page.get_by_label("Unread", exact=True).uncheck()
        await page.get_by_label("Filter changes by course").select_option("")
        await expect(page.locator(".change-entry")).to_have_count(5)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "changes-mobile.png"), full_page=True)
        assert not errors, errors
        assert summary(Database(db.path))["unread_count"] == 3
        await browser.close()


def main():
    handler = partial(QuietHandler, directory=str(Path("frontend/dist").resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="coursedeck-changes-smoke-") as directory:
            asyncio.run(check(f"http://127.0.0.1:{server.server_port}", Path(directory)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("Changes UI passed: before/after, persisted read state, filters, duplicate sync, mobile.")


if __name__ == "__main__":
    main()
