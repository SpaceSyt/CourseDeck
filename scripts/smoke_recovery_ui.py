"""Exercise paired recovery UI against a disposable backend and synthetic databases."""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, Settings, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.recovery import digest, inspect_database, write_manifest
from scripts.smoke_support import stop_backend


def seed(folder):
    db = Database(folder / "coursedeck.sqlite3")
    db.save_settings(Settings(startup_sync=False))
    course = Course(provider="google_classroom", external_id="fixture", name="Recovery fixture")
    task = Task(
        provider=course.provider,
        course_external_id=course.external_id,
        external_id="task",
        title="Recovery task",
    )
    db.apply(
        course.provider,
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    db.patch_local(task.id, {"note": "Original note"})
    knowledge = KnowledgeStore(folder / "knowledge.sqlite3")
    document = {
        "id": "fixture-material",
        "provider": course.provider,
        "course_id": course.id,
        "title": "Recovery material",
        "body": "Original knowledge",
        "complete": True,
    }
    knowledge.upsert_documents([document])
    backup_folder = folder / "backups"
    backup_folder.mkdir()
    db.backup(backup_folder / ("coursedeck-legacy-" + "very-long-" * 16 + ".sqlite3"))
    return db, knowledge, task.id, document


async def start(folder, port):
    process = subprocess.Popen(
        [sys.executable, "-m", "coursedeck", "--port", str(port)],
        env=os.environ | {"COURSEDECK_DATA_DIR": str(folder)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            for _ in range(100):
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/api/heartbeat")
                    if response.status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Disposable recovery backend stopped during startup")
                await asyncio.sleep(0.1)
        raise RuntimeError("Disposable recovery backend did not start")
    except BaseException:
        stop_backend(process)
        raise


def stop(process):
    stop_backend(process)


async def main():
    with tempfile.TemporaryDirectory(prefix="coursedeck-recovery-ui-") as temporary:
        folder = Path(temporary)
        db, knowledge, task_id, document = seed(folder)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        process = await start(folder, port)
        try:
            async with (
                async_playwright() as playwright,
                httpx.AsyncClient(base_url=base_url) as api,
            ):
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={"width": 1440, "height": 1050})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base_url)
                await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
                panel = page.locator("section").filter(
                    has=page.get_by_role("heading", name="Backup & restore", exact=True)
                )
                await panel.get_by_role("button", name="Back up now", exact=True).click()
                await expect(panel.get_by_role("status")).to_have_text("Paired backup saved.")
                backups = (await api.get("/api/recovery/backups")).json()["backups"]
                original = next(item["id"] for item in backups if item.get("reason") == "manual")
                # Simulate the previous, unregistered schema in the disposable
                # backup only. Preparation must publish a new pair, never rewrite it.
                old_folder = folder / "backups" / original
                manifest_path = old_folder / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for name in ("coursedeck.sqlite3", "knowledge.sqlite3"):
                    path = old_folder / name
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("DROP TABLE schema_migrations")
                        if name == "knowledge.sqlite3":
                            connection.execute("DROP TABLE material_checkpoints")
                    manifest["files"][name] = {
                        "sha256": digest(path),
                        "schema": inspect_database(path),
                        "bytes": path.stat().st_size,
                    }
                write_manifest(manifest_path, manifest)
                originals = {
                    path.name: digest(path) for path in old_folder.iterdir() if path.is_file()
                }
                await page.reload()
                await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
                await panel.get_by_label("Backup", exact=True).select_option(original)
                await panel.get_by_role("button", name="Prepare upgraded copy", exact=True).click()
                await expect(panel.get_by_role("status")).to_have_text(
                    "Backup copy prepared. Original files retained."
                )
                await expect(
                    panel.get_by_role("button", name="Restore this backup", exact=True)
                ).to_be_enabled()
                prepared = await panel.get_by_label("Backup", exact=True).input_value()
                assert prepared and prepared != original
                assert originals == {name: digest(old_folder / name) for name in originals}
                assert db.tasks()[0]["local"]["note"] == "Original note"
                original = prepared
                await panel.get_by_label("Backup", exact=True).select_option("")
                db.patch_local(task_id, {"note": "Changed note"})
                knowledge.upsert_documents([document | {"body": "Changed knowledge"}])
                await panel.get_by_label("Backup", exact=True).select_option(original)
                await expect(
                    panel.get_by_role("button", name="Restore this backup", exact=True)
                ).to_have_count(0)
                await panel.get_by_role("button", name="Check backup", exact=True).click()
                await expect(panel).to_contain_text("Current data will be backed up first.")
                assert (
                    next(task for task in db.tasks() if task["id"] == task_id)["local"]["note"]
                    == "Changed note"
                )
                assert knowledge.get(document["id"])["body"] == "Changed knowledge"
                screenshot = Path("data/debug/recovery-preflight-fixture.png")
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                await expect(panel.locator("summary")).to_have_text("1 unavailable backups")
                await page.screenshot(
                    path="data/debug/recovery-long-backup-mobile-fixture.png", full_page=True
                )
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await panel.locator("summary").click()
                await expect(panel.locator("details")).to_contain_text(
                    "Unrecognized legacy backup name"
                )
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await panel.locator("summary").click()
                await expect(
                    panel.get_by_role("button", name="Restore this backup", exact=True)
                ).to_be_visible()
                await page.set_viewport_size({"width": 1440, "height": 1050})
                await panel.get_by_role("button", name="Restore this backup", exact=True).click()
                await expect(
                    page.locator(".task-row").filter(has_text="Recovery task")
                ).to_be_visible()
                assert (
                    next(task for task in db.tasks() if task["id"] == task_id)["local"]["note"]
                    == "Original note"
                )
                assert knowledge.get(document["id"])["body"] == "Original knowledge"
                backups = (await api.get("/api/recovery/backups")).json()["backups"]
                previous = next(
                    item["id"] for item in backups if item.get("reason") == "pre_restore"
                )

                await page.goto("about:blank")
                knowledge.close()
                stop(process)
                db.patch_local(task_id, {"note": "Interrupted note"})
                knowledge.upsert_documents([document | {"body": "Interrupted knowledge"}])
                journal = folder / "backups/restore-pending.json"
                journal.write_text(json.dumps({"target": original, "rollback": previous}))
                process = await start(folder, port)
                assert (await api.get("/api/snapshot")).status_code == 503
                await page.goto(base_url)
                panel = page.locator("section").filter(
                    has=page.get_by_role("heading", name="Backup & restore", exact=True)
                )
                await expect(panel).to_be_visible()
                await expect(panel).to_contain_text("A restore was interrupted.")
                await expect(
                    panel.get_by_role("button", name="Back up now", exact=True)
                ).to_be_disabled()
                await panel.get_by_label("Backup", exact=True).select_option(original)
                await panel.get_by_role("button", name="Check backup", exact=True).click()
                await panel.get_by_role("button", name="Restore this backup", exact=True).click()
                await expect(
                    page.locator(".task-row").filter(has_text="Recovery task")
                ).to_be_visible()
                assert not journal.exists()
                assert (await api.get("/api/snapshot")).status_code == 200
                assert (
                    next(task for task in db.tasks() if task["id"] == task_id)["local"]["note"]
                    == "Original note"
                )
                assert knowledge.get(document["id"])["body"] == "Original knowledge"
                assert not errors, errors
                await browser.close()
        finally:
            knowledge.close()
            stop(process)
    print(
        "Recovery UI passed: paired backup, preserved-original schema upgrade, "
        "read-only preflight, note/knowledge restore, "
        "pre-restore backup, interrupted-startup recovery and resumed workspace."
    )


if __name__ == "__main__":
    asyncio.run(main())
