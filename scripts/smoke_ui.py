"""Browser checks use a temporary backend, never the user's live database."""

import asyncio
import os
import re
import socket
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright, expect

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, Settings, SyncResult, Task, now
from coursedeck.mail import MailMessage, MailStore


async def check_ui(base_url):
    Path("data/debug").mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(base_url)
        await expect(page.get_by_role("heading", name="Assignments", exact=True)).to_be_visible()
        connection = page.locator('.topbar [role="status"]')
        await expect(connection).to_have_attribute("aria-label", "Local service: connected")
        await expect(page.locator(".task-row")).to_have_count(1)
        assert await page.locator(".privacy, .app-footer").count() == 0
        assert (
            await page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
        )
        await check_task_completion(page)
        await check_source_status_views(page)
        await page.locator("nav").get_by_role("button", name="Courses", exact=True).click()
        cards = page.locator(".course-card")
        await expect(cards).to_have_count(2)
        codes = await cards.locator(".course-code").evaluate_all(
            "els => els.map(el => ({text: el.textContent, "
            "height: el.getBoundingClientRect().height}))"
        )
        assert codes[0]["text"] and not codes[1]["text"]
        assert abs(codes[0]["height"] - codes[1]["height"]) < 1
        footers = await cards.locator("footer").evaluate_all(
            "els => els.map(el => el.getBoundingClientRect().top)"
        )
        assert abs(footers[0] - footers[1]) < 1
        await page.screenshot(path="data/debug/course-alignment.png", full_page=True)
        await page.locator("nav").get_by_role("button", name="Todo", exact=False).click()
        await page.locator(".task-main").click()
        previous_note = await page.get_by_placeholder("Add a note…").input_value()
        await page.get_by_placeholder("Add a note…").fill("Keep this note")
        await page.get_by_role("button", name="Save note", exact=True).click()
        await expect(page.get_by_role("button", name="Save note", exact=True)).to_be_disabled()
        history = page.locator(".task-edit-history")
        await history.locator("summary").click()
        await expect(history).to_contain_text("note")
        await history.get_by_role("button", name="Undo", exact=True).click()
        await expect(page.get_by_placeholder("Add a note…")).to_have_value(previous_note)
        await expect(history).to_contain_text("Undone")
        await page.get_by_placeholder("Add a note…").fill("Keep this note")
        await page.get_by_role("button", name="Save note", exact=True).click()
        await expect(page.get_by_role("button", name="Save note", exact=True)).to_be_disabled()
        await page.get_by_role("button", name="Close details").click()
        await page.get_by_role("button", name="Add course", exact=True).click()
        dialog = page.locator(".course-dialog")
        await dialog.get_by_label("Course name", exact=True).fill("My Biology")
        await dialog.get_by_role("button", name="Add course", exact=True).click()
        await expect(dialog).to_have_count(0)
        await page.reload()
        await page.locator(".course-nav-item").filter(has_text="My Biology").click()
        await page.get_by_role("button", name="Link source course", exact=True).click()
        await dialog.get_by_label("Source course", exact=True).select_option("google_classroom:123")
        await dialog.get_by_role("button", name="Link course", exact=True).click()
        await expect(dialog).to_have_count(0)
        await expect(page.locator(".task-row")).to_have_count(1)
        await page.locator("nav").get_by_role("button", name="Courses", exact=True).click()
        card = page.locator(".course-card").filter(has_text="My Biology")
        await card.get_by_role("button", name="Edit course", exact=True).click()
        await dialog.get_by_label("Source", exact=True).select_option("gradescope")
        await dialog.get_by_label("Source course", exact=True).select_option("gradescope:789")
        await dialog.get_by_role("button", name="Add link", exact=True).click()
        await expect(dialog.locator(".course-link-list > div")).to_have_count(2)
        await dialog.get_by_role("button", name="Save", exact=True).click()
        await expect(dialog).to_have_count(0)
        await expect(page.locator(".course-card")).to_have_count(1)
        await expect(card).to_contain_text("Classroom · Gradescope")
        await card.get_by_role("button", name="Edit course", exact=True).click()
        await expect(dialog.locator(".course-link-list > div")).to_have_count(2)
        await page.screenshot(path="data/debug/course-multiple-sources.png", full_page=True)
        await dialog.get_by_role("button", name="Cancel", exact=True).click()
        await expect(card.get_by_label("Alias enabled", exact=True)).to_be_visible()
        await card.get_by_role("button", name="Edit course", exact=True).click()
        await dialog.get_by_role("button", name="Clear alias", exact=True).click()
        await dialog.get_by_role("button", name="Save", exact=True).click()
        card = page.locator(".course-card").filter(has_text="Biology fixture")
        await expect(
            card.get_by_role("heading", name="Biology fixture", exact=True)
        ).to_be_visible()
        await expect(card.get_by_label("Alias enabled", exact=True)).to_have_count(0)
        await card.get_by_role("button", name="Edit course", exact=True).click()
        await dialog.get_by_label("Alias", exact=True).fill("My Biology")
        await dialog.get_by_role("button", name="Save", exact=True).click()
        card = page.locator(".course-card").filter(has_text="My Biology")
        await card.get_by_role("button", name="Disable", exact=True).click()
        await expect(card.get_by_role("button", name="Enable", exact=True)).to_be_visible()
        await page.locator("nav").get_by_role("button", name="Todo", exact=False).click()
        await expect(page.locator(".task-row")).to_have_count(0)
        await expect(page.locator(".course-nav-item")).to_have_count(0)
        await page.get_by_role(
            "button", name="Open email: Biology fixture survey", exact=True
        ).click()
        await page.get_by_role("button", name="Add to do", exact=True).click()
        await expect(page.locator(".task-dialog").get_by_label("Course", exact=True)).to_have_value(
            ""
        )
        await page.get_by_role("button", name="Close task dialog", exact=True).click()
        await page.get_by_role("button", name="Back to inbox", exact=True).click()
        await page.locator("nav").get_by_role("button", name="Courses", exact=True).click()
        await card.get_by_role("button", name="Enable", exact=True).click()
        await card.get_by_role("button", name="Delete", exact=True).click()
        await expect(page.locator(".course-card")).to_have_count(0)
        await page.get_by_label("Show deleted", exact=True).check()
        await expect(card).to_be_visible()
        await card.get_by_role("button", name="Restore course", exact=True).click()
        await page.get_by_label("Show deleted", exact=True).uncheck()
        await expect(card.get_by_label("Alias enabled", exact=True)).to_be_visible()
        await page.screenshot(path="data/debug/course-management.png", full_page=True)
        await page.locator("nav").get_by_role("button", name="Todo", exact=False).click()
        await expect(page.locator(".task-row")).to_have_count(1)
        await page.locator(".task-main").click()
        await expect(page.get_by_placeholder("Add a note…")).to_have_value("Keep this note")
        await page.get_by_role("button", name="Close details").click()
        await page.route("**/api/heartbeat", lambda route: route.abort())
        await page.route("**/api/snapshot", lambda route: route.abort())
        await expect(connection).to_have_attribute(
            "aria-label", "Local service: disconnected", timeout=10000
        )
        await expect(page.locator(".task-row")).to_have_count(1)
        await page.unroute("**/api/heartbeat")
        await page.unroute("**/api/snapshot")
        await expect(connection).to_have_attribute(
            "aria-label", "Local service: connected", timeout=10000
        )
        release = asyncio.Event()

        async def hang(route):
            await release.wait()
            try:
                await route.abort()
            except BrowserError as exc:
                if "already handled" not in str(exc):
                    raise

        await page.route("**/api/heartbeat", hang)
        await expect(connection).to_have_attribute(
            "aria-label", "Local service: disconnected", timeout=10000
        )
        release.set()
        await page.unroute("**/api/heartbeat")
        await expect(connection).to_have_attribute(
            "aria-label", "Local service: connected", timeout=10000
        )
        output = Path("data/debug")
        output.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(output / "todo-desktop.png"), full_page=True)
        await page.locator("nav").get_by_role("button", name="Sources", exact=True).click()
        await expect(page.locator(".source-card")).to_have_count(5)
        await expect(
            page.locator("#source-rephactor").get_by_role("button", name="Connect", exact=True)
        ).to_be_visible()
        classroom = page.locator("#source-google_classroom")
        await expect(classroom.get_by_role("button", name="Connect", exact=True)).to_be_visible()
        await classroom.get_by_role("button", name="Configure Google Classroom").click()
        await expect(classroom.get_by_label("Connection method")).to_have_value("browser")
        assert await classroom.get_by_label("Desktop OAuth client ID").count() == 0
        await classroom.get_by_role("button", name="Configure Google Classroom").click()
        await page.screenshot(path=str(output / "sources-desktop.png"), full_page=True)
        await page.locator("nav").get_by_role("button", name="Todo", exact=False).click()
        await page.get_by_role("button", name="Add task", exact=True).click()
        task_dialog = page.locator(".task-dialog")
        await task_dialog.get_by_label("Title", exact=True).fill("Buy notebooks")
        await task_dialog.get_by_role("button", name="Add task", exact=True).click()
        await expect(task_dialog).to_have_count(0)
        await expect(page.locator(".task-row")).to_have_count(2)
        await expect(page.locator(".mail-row")).to_have_count(3)
        await expect(page.locator(".mail-dot.none")).to_have_count(1)
        await expect(page.locator(".mail-dot.unclassifiable")).to_have_count(1)
        await page.get_by_role(
            "button", name="Open email: Biology fixture survey", exact=True
        ).click()
        await expect(page.locator(".mail-body")).to_contain_text("Please complete the survey")
        sidebar_color = (
            await page.locator(".course-nav-item")
            .filter(has_text="My Biology")
            .locator(".dot")
            .evaluate("el => getComputedStyle(el).backgroundColor")
        )
        mail_color = await page.locator(".mail-course .mail-dot").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        )
        assert sidebar_color == mail_color
        await page.get_by_role("button", name="Add to do", exact=True).click()
        await expect(task_dialog.get_by_label("Course", exact=True)).to_have_value(
            "google_classroom:123"
        )
        await task_dialog.get_by_label("Title", exact=True).fill("Complete survey")
        await task_dialog.get_by_role("button", name="Add task", exact=True).click()
        await expect(task_dialog).to_have_count(0)
        await expect(page.locator(".task-row")).to_have_count(3)
        await page.get_by_role("button", name="Delete email locally").click()
        await page.get_by_role("button", name="Back to inbox").click()
        await expect(page.locator(".mail-row")).to_have_count(2)
        await page.get_by_label("Inbox menu", exact=True).click()
        await page.get_by_label("Show deleted", exact=True).check()
        await expect(page.locator(".mail-row")).to_have_count(3)
        await page.get_by_label("Inbox menu", exact=True).click()
        await page.get_by_role(
            "button", name="Open email: Biology fixture survey", exact=True
        ).click()
        await page.get_by_role("button", name="Restore email", exact=True).click()
        await page.get_by_role("button", name="Back to inbox").click()
        await page.get_by_label("Inbox menu", exact=True).click()
        await page.get_by_role("button", name="Mail rules", exact=True).click()
        rules = page.locator(".mail-rules")
        await rules.get_by_label("Contains", exact=True).fill("Campus newsletter")
        await rules.get_by_label("Apply", exact=True).select_option("ignore")
        await rules.get_by_role("button", name="Preview changes", exact=True).click()
        impact = rules.get_by_role("region", name="Rule impact", exact=True)
        await expect(impact).to_contain_text("1 newly ignored")
        await expect(
            rules.locator(".rule-list > div").filter(has_text="Campus newsletter")
        ).to_have_count(0)
        await impact.get_by_role("button", name="Apply changes", exact=True).click()
        await expect(
            rules.locator(".rule-list > div").filter(has_text="Campus newsletter")
        ).to_have_count(1)
        await rules.get_by_role("button", name="Done", exact=True).click()
        await expect(page.locator(".mail-row")).to_have_count(2)
        await page.get_by_label("Inbox menu", exact=True).click()
        await page.get_by_label("Show auto-ignored", exact=True).check()
        await expect(page.locator(".mail-row")).to_have_count(3)
        await page.get_by_label("Inbox menu", exact=True).click()
        await page.get_by_role("button", name="Open email: Campus newsletter", exact=True).click()
        await page.get_by_role("button", name="Auto-ignored · Restore", exact=True).click()
        await page.get_by_role("button", name="Back to inbox").click()
        await page.reload()
        await expect(page.locator(".task-row")).to_have_count(3)
        await expect(page.locator(".mail-row")).to_have_count(3)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "inbox-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        for name in ("Sources", "Courses", "Settings", "Todo"):
            await page.locator("nav").get_by_role("button", name=name, exact=name != "Todo").click()
            await expect(page.locator("h1")).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        await page.screenshot(path=str(output / "todo-mobile.png"), full_page=True)
        await page.get_by_role("button", name="Inbox", exact=True).click()
        await expect(page.locator(".inbox-rail")).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "inbox-mobile.png"), full_page=True)
        await page.get_by_role("button", name="Close inbox").click()
        await page.get_by_role("button", name="Add course", exact=True).click()
        await expect(dialog).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "course-mobile.png"), full_page=True)
        assert not errors, errors
        await browser.close()


async def check_task_completion(page):
    row = page.locator(".task-row")
    await expect(row.locator(".task-source-warning")).to_have_text("Status unknown")
    await expect(row).to_have_class(re.compile(r"\buncertain\b"))

    async def fail_save(route):
        await route.fulfill(status=500, json={"detail": "Test save failed"})

    await page.route("**/api/tasks/*/local", fail_save)
    await row.get_by_role("button", name="Complete Read chapter 2", exact=True).click()
    await expect(page.locator(".notice")).to_contain_text("Test save failed")
    await expect(row).to_have_count(1)
    await expect(row.locator(".checkbox")).to_have_attribute("aria-pressed", "false")
    await expect(row).not_to_have_class(re.compile(r"\bcompleting\b"))
    await page.unroute("**/api/tasks/*/local", fail_save)

    await page.evaluate(
        """() => {
            window.taskAnimations = [];
            document.addEventListener('animationstart', event => {
                if (event.animationName.startsWith('task-')) {
                    window.taskAnimations.push(event.animationName);
                }
            });
        }"""
    )
    release = asyncio.Event()

    async def delay_save(route):
        await release.wait()
        await route.continue_()

    await page.route("**/api/tasks/*/local", delay_save)
    await row.get_by_role("button", name="Complete Read chapter 2", exact=True).click()
    await expect(row).to_have_attribute("aria-busy", "true")
    await expect(row.locator(".checkbox")).to_have_attribute("aria-pressed", "true")
    await expect(row.locator(".task-title-text")).to_have_css("animation-name", "task-strike")
    # The row must stay visible until the server actually accepts completion.
    assert await row.is_visible()
    release.set()
    await expect(row).to_have_count(0)
    await page.unroute("**/api/tasks/*/local", delay_save)
    assert await page.evaluate("window.taskAnimations") == ["task-strike", "task-exit"]
    await page.get_by_role("button", name="Done", exact=True).click()
    await expect(row).to_have_count(1)
    await row.get_by_role("button", name="Restore Read chapter 2", exact=True).click()
    await expect(row).to_have_count(0)
    await page.get_by_role("button", name="To do", exact=True).click()
    await expect(row).to_have_count(1)

    await page.emulate_media(reduced_motion="reduce")
    await page.evaluate("window.taskAnimations = []")
    await row.get_by_role("button", name="Complete Read chapter 2", exact=True).click()
    await expect(row).to_have_count(0)
    assert await page.evaluate("window.taskAnimations") == []
    await page.get_by_role("button", name="Done", exact=True).click()
    await row.get_by_role("button", name="Restore Read chapter 2", exact=True).click()
    await page.get_by_role("button", name="To do", exact=True).click()
    await expect(row).to_have_count(1)
    await page.emulate_media(reduced_motion="no-preference")


async def check_source_status_views(page):
    from coursedeck.task_state import project_task_state

    async def source_states(route):
        # This route changes the representation, so the backend's ETag is not ours.
        response = await route.fetch(
            headers={
                key: value
                for key, value in route.request.headers.items()
                if key.lower() != "if-none-match"
            }
        )
        snapshot = await response.json()
        original = snapshot["tasks"][0]
        snapshot["tasks"] += [
            original
            | {
                "id": f"fixture-{availability}-{known}",
                "title": title,
                "submission_status": "submitted",
                "source_availability": availability,
                "source_status_known": known,
            }
            for title, availability, known in [
                ("Source confirmed", "present", True),
                ("Source missing", "missing", True),
                ("Source stale", "present", False),
                ("Source unconfirmed", "unconfirmed", False),
            ]
        ]
        for task in snapshot["tasks"]:
            task.update(project_task_state(task))
        await route.fulfill(response=response, json=snapshot)

    await page.route("**/api/snapshot", source_states)
    await page.reload()
    await expect(page.locator(".task-row")).to_have_count(4)
    for title, label in [
        ("Source missing", "Not found in source"),
        ("Source stale", "Status unknown"),
        ("Source unconfirmed", "Not refreshed"),
    ]:
        row = page.locator(".task-row").filter(has_text=title)
        await expect(row.locator(".task-source-warning")).to_have_text(label)
        await expect(row.locator(".checkbox")).to_have_attribute("aria-pressed", "false")
    await page.get_by_role("button", name="Done", exact=True).click()
    await expect(page.locator(".task-row")).to_have_count(1)
    await expect(
        page.get_by_role("button", name="Completed at source: Source confirmed", exact=True)
    ).to_be_disabled()
    await page.unroute("**/api/snapshot", source_states)
    await page.reload()
    await expect(page.locator(".task-row")).to_have_count(1)


async def main():
    with tempfile.TemporaryDirectory(prefix="coursedeck-smoke-") as folder:
        db = Database(Path(folder) / "coursedeck.sqlite3")
        db.save_settings(Settings(startup_sync=False))
        course = Course(
            provider="google_classroom",
            external_id="123",
            name="Biology fixture",
            section="BIO-101",
        )
        task = Task(
            provider=course.provider,
            course_external_id=course.external_id,
            external_id="456",
            title="Read chapter 2",
            due_at=now() + timedelta(days=1),
        )
        db.apply(
            course.provider,
            SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
            now().isoformat(),
        )
        db.apply(
            "gradescope",
            SyncResult(
                outcome=Outcome.SUCCESS,
                courses=[Course(provider="gradescope", external_id="789", name="Biology lab")],
            ),
            now().isoformat(),
        )
        mail = MailStore(db)
        for index, subject in enumerate(
            ["Biology fixture survey", "Campus newsletter", "Unknown notice"]
        ):
            mail.upsert(
                MailMessage(
                    id=f"mail-{index}",
                    sender="University",
                    sender_email="test@example.test",
                    subject=subject,
                    snippet="Please complete the survey before Friday.",
                    body="Please complete the survey before Friday. <script>alert(1)</script>",
                    body_complete=index != 2,
                    date_label="Sep 8, 2026, 13:21",
                    date_text="13:21",
                    unread=True,
                )
            )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "coursedeck", "--port", str(port)],
            env=os.environ | {"COURSEDECK_DATA_DIR": folder},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(timeout=1) as client:
                for _ in range(100):
                    try:
                        response = await client.get(base_url + "/api/heartbeat")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Isolated backend did not start")
            await check_ui(base_url)
        finally:
            from scripts.smoke_support import stop_backend

            stop_backend(process)
    print(
        "Browser smoke passed: course alignment/binding, source status views, completion "
        "animation/save failure/reduced motion, notes, heartbeat recovery, dark UI, mobile."
    )


if __name__ == "__main__":
    asyncio.run(main())
