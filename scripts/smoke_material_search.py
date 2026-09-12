"""Deterministic materials UI checks against a temporary database and browser profile.

Build the frontend first, then run: python scripts/smoke_material_search.py
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, Settings, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore


async def run():
    Path("data/debug").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coursedeck-material-ui-") as directory:
        folder = Path(directory)
        db = Database(folder / "coursedeck.sqlite3")
        db.save_settings(Settings(startup_sync=False))
        course = Course(provider="brightspace", external_id="math", name="Mathematics fixture")
        task = Task(
            provider=course.provider,
            course_external_id="math",
            external_id="hw",
            title="Algebra homework",
            submission_status="not_submitted",
        )
        db.apply(
            "brightspace",
            SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
            now().isoformat(),
        )
        store = KnowledgeStore(folder / "knowledge.sqlite3")
        current = now()
        base = {
            "provider": "brightspace",
            "course_id": course.id,
            "kind": "material",
            "url": "https://school.example/material",
            "complete": True,
            "fetched_at": current.isoformat(),
            "checked_at": current.isoformat(),
            "source_modified_at": (current - timedelta(days=3)).isoformat(),
        }
        store.upsert_documents(
            [
                base
                | {
                    "id": "notes",
                    "title": "Algebra lecture",
                    "body": "intro " * 3000 + "TAILMARKER final instructions",
                    "source_task_id": task.id,
                },
                base
                | {
                    "id": "partial",
                    "title": "Deadline announcement",
                    "body": "Use the revised deadline.",
                    "kind": "announcement",
                    "complete": False,
                    "fetched_at": (current - timedelta(days=9)).isoformat(),
                    "warnings": ["One attachment unavailable"],
                },
            ]
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "coursedeck", "--port", str(port)],
            env=os.environ | {"COURSEDECK_DATA_DIR": directory},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(timeout=1) as client:
                for _ in range(100):
                    try:
                        if (await client.get(base_url + "/api/heartbeat")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Backend unavailable")
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base_url)
                await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
                await page.get_by_role("button", name="Materials", exact=True).click()
                await expect(page.locator(".material-list > button")).to_have_count(2)
                await page.get_by_label("Search materials", exact=True).fill("TAILMARKER")
                await expect(page.locator(".material-list > button")).to_have_count(1)
                await expect(page.locator(".material-preview")).to_contain_text("TAILMARKER")
                await page.locator(".material-list > button").click()
                await expect(page.locator(".material-body")).to_contain_text("TAILMARKER")
                await expect(page.locator(".material-times")).to_contain_text("Source updated")
                await expect(page.locator(".material-times")).to_contain_text("Read")
                await page.get_by_role("button", name="Back to materials").click()
                await page.get_by_label("Search materials", exact=True).fill("")
                await page.get_by_label("Material source", exact=True).select_option("brightspace")
                await page.get_by_label("Material type", exact=True).select_option("announcement")
                await page.get_by_label("Material read time", exact=True).select_option("stale")
                await page.get_by_label("Material completeness", exact=True).select_option(
                    "incomplete"
                )
                await expect(page.locator(".material-list > button")).to_have_count(1)
                await expect(page.locator(".material-list > button")).to_contain_text(
                    "Deadline announcement"
                )
                await page.locator(".material-list > button").click()
                await expect(page.locator(".material-detail")).to_contain_text(
                    "One attachment unavailable"
                )
                await expect(page.locator(".material-times")).to_contain_text("Checked")
                await page.screenshot(path="data/debug/material-filters-desktop.png")
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.screenshot(path="data/debug/material-filters-mobile.png")
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.locator("nav").get_by_role("button", name="Todo", exact=False).click()
                await page.locator(".task-main").click()
                await page.get_by_role("button", name="Ask about task", exact=True).click()
                await expect(page.locator(".chat-task-context > strong")).to_have_text(
                    "Algebra homework"
                )
                await expect(page.get_by_label("Chat course", exact=True)).to_have_value(course.id)
                await page.get_by_text("Related materials (1)", exact=True).click()
                await page.locator(".chat-task-context details button").click()
                await expect(page.locator(".material-detail h2")).to_have_text("Algebra lecture")
                await expect(page.locator(".material-body")).to_contain_text("TAILMARKER")
                assert not errors, errors
                await browser.close()
            print(
                "Material UI smoke passed: full text tail search, four filters, partial warning, "
                "source/read/check times, mobile overflow, task context and linked document."
            )
        finally:
            from scripts.smoke_support import stop_backend

            store.close()
            stop_backend(process)


if __name__ == "__main__":
    asyncio.run(run())
