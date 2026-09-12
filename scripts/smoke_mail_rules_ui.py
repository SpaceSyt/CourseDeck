"""Browser check of mail rule previews against a synthetic temporary local database."""

import asyncio
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from coursedeck.app import create_app
from coursedeck.domain import Course, Outcome, SyncResult, now
from coursedeck.mail import MailMessage, MailStore


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def check(base_url, app):
    db, store = app.state.db, MailStore(app.state.db)
    course = Course(provider="brightspace", external_id="fixture", name="Programming fixture")
    db.apply(
        "brightspace", SyncResult(outcome=Outcome.SUCCESS, courses=[course]), now().isoformat()
    )
    for key, subject, body, date in [
        ("priority", "Survey invitation", "Please reply to the survey.", "2026-09-01T12:00:00Z"),
        ("normal", "Weekly course update", "Read chapter one.", "2026-09-10T12:00:00Z"),
    ]:
        store.upsert(
            MailMessage(
                id=key,
                sender="Instructor fixture",
                sender_email="instructor@example.test",
                subject=subject,
                body=body,
                snippet=body,
                body_complete=True,
                received_at=date,
            )
        )
    client = TestClient(app, base_url="http://127.0.0.1")
    writes = []

    async def route_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        if (
            request.method != "GET"
            and parsed.path.startswith("/api/mail/rules")
            and parsed.path != "/api/mail/rules/preview"
        ):
            writes.append((request.method, parsed.path))
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
        page = await browser.new_page(viewport={"width": 1500, "height": 1050})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/**", route_api)
        await page.goto(base_url)
        inbox = page.get_by_role("complementary", name="Inbox", exact=True)
        await expect(inbox.locator(".mail-row")).to_have_count(2)
        await expect(inbox.locator(".mail-subject").first).to_have_text("Weekly course update")
        await inbox.get_by_label("Inbox menu", exact=True).click()
        await inbox.get_by_label("Priority first", exact=True).check()
        await expect(inbox.locator(".mail-subject").first).to_have_text("Survey invitation")
        await inbox.get_by_label("Priority only", exact=True).check()
        await expect(inbox.locator(".mail-row")).to_have_count(1)
        await expect(inbox.get_by_label("Clear priority filter", exact=True)).to_be_visible()
        await inbox.get_by_label("Priority only", exact=True).uncheck()
        await inbox.get_by_label("Inbox menu", exact=True).click()
        await inbox.get_by_label("Sort emails by newest", exact=True).click()
        await expect(inbox.locator(".mail-subject").first).to_have_text("Weekly course update")
        await inbox.get_by_role(
            "button", name="Open email: Weekly course update", exact=True
        ).click()
        await inbox.get_by_role("button", name="Create rule", exact=True).click()
        dialog = page.locator("dialog.mail-rules")
        await expect(dialog.get_by_label("Contains", exact=True)).to_have_value(
            "instructor@example.test"
        )
        await dialog.get_by_label("Match in", exact=True).select_option("subject")
        await expect(dialog.get_by_label("Contains", exact=True)).to_have_value(
            "Weekly course update"
        )
        await dialog.get_by_label("Apply", exact=True).select_option("ignore")
        await dialog.get_by_role("button", name="Preview changes", exact=True).click()
        review = dialog.get_by_role("region", name="Rule impact", exact=True)
        await expect(review).to_contain_text("1 newly ignored")
        assert writes == []
        assert not store.get("normal")["ignored"]
        await dialog.get_by_label("Contains", exact=True).fill("No matching subject")
        await expect(review).to_have_count(0)
        await dialog.get_by_label("Contains", exact=True).fill("Weekly course update")
        await dialog.get_by_role("button", name="Preview changes", exact=True).click()
        await review.get_by_role("button", name="Apply changes", exact=True).click()
        await expect(dialog.get_by_label("Contains", exact=True)).to_have_value("")
        assert writes == [("POST", "/api/mail/rules")]
        assert store.get("normal")["ignored"]
        await dialog.get_by_role("button", name="Done", exact=True).click()
        await inbox.get_by_role("button", name="Back to inbox", exact=True).click()
        await expect(inbox.locator(".mail-row")).to_have_count(1)
        await inbox.get_by_label("Inbox menu", exact=True).click()
        await inbox.get_by_label("Show auto-ignored", exact=True).check()
        await inbox.get_by_label("Inbox menu", exact=True).click()
        await expect(inbox.locator(".mail-row")).to_have_count(2)
        await inbox.get_by_role(
            "button", name="Open email: Weekly course update", exact=True
        ).click()
        await inbox.get_by_role("button", name="Auto-ignored · Restore", exact=True).click()
        await expect(
            inbox.get_by_role("button", name="Auto-ignored · Restore", exact=True)
        ).to_have_count(0)
        assert not store.get("normal")["ignored"]
        await inbox.get_by_role("button", name="Create rule", exact=True).click()
        await dialog.get_by_label("Enable rule Weekly course update", exact=True).click()
        await expect(review).to_contain_text("manual choices")
        assert len(writes) == 1
        await review.get_by_role("button", name="Apply changes", exact=True).click()
        await expect(
            dialog.get_by_label("Enable rule Weekly course update", exact=True)
        ).not_to_be_checked()
        assert writes[-1][0] == "PUT"
        await dialog.get_by_role(
            "button", name="Delete rule Weekly course update", exact=True
        ).click()
        await expect(review.get_by_role("heading")).to_have_text("Delete rule")
        assert len(writes) == 2
        await review.get_by_role("button", name="Cancel", exact=True).click()
        assert len(writes) == 2
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        await browser.close()


def main():
    handler = partial(QuietHandler, directory=str(Path("frontend/dist").resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="coursedeck-mail-ui-") as directory:
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
        "Mail rules UI passed: sender/subject prefill, read-only impact, "
        "stale preview invalidation, apply, ignore restore, "
        "toggle/delete review, priority controls."
    )


if __name__ == "__main__":
    main()
