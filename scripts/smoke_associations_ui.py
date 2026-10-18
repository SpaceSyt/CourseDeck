"""Run TaskRelations browser checks using synthetic data and an isolated HTTP server.

Run from the repository root: python scripts/smoke_associations_ui.py
Requires frontend npm dependencies and Playwright Chromium. No app server, real
database, dedicated login profile, or external network is used. Screenshots are
written to data/debug/associations-{desktop,mobile}.png.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]

HARNESS = """
import {createRoot} from 'react-dom/client';
import {TaskRelations} from './src/TaskRelations';
import './src/style.css';
import './src/dark.css';
const task = {
  id:'task1', title:'Homework 1',
  description:'Read the instructions and submit your work.',
  url:'https://example.test/assignment/1?token=synthetic-secret',
  last_seen_at:'2026-09-10T12:00:00Z', provider:'gradescope'
};
const opened = (id) => { window.opened = id; };
createRoot(document.getElementById('root')).render(
  <TaskRelations task={task} onOpenTask={opened}
    onOpenMail={opened} onOpenDocument={opened}/>
);
"""

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Task relations smoke fixture</title>
<link rel="stylesheet" href="association-ui.css"></head>
<body style="padding:32px;background:#10151d;color:#e4eaf3">
<main id="root" style="max-width:720px;margin:auto"></main>
<script src="association-ui.js"></script></body></html>
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def fixture():
    return {
        "task_id": "task1",
        "relations": [
            {
                "id": "r1",
                "state": "suggested",
                "active": True,
                "target": {
                    "kind": "task",
                    "id": "task2",
                    "title": "Homework 1",
                    "provider": "webassign",
                    "status": "open",
                    "url": "javascript:alert(1)",
                },
                "evidence": [{"kind": "assignment_number", "label": "Same course and homework:1"}],
                "history": [],
            },
            {
                "id": "r2",
                "state": "automatic",
                "active": True,
                "target": {
                    "kind": "document",
                    "id": "doc",
                    "title": "Homework instructions",
                    "provider": "brightspace",
                    "status": "Incomplete",
                    "url": None,
                },
                "evidence": [{"kind": "source_id", "label": "Explicit source reference"}],
                "history": [],
            },
        ],
        "available": [
            {
                "kind": "mail",
                "id": "mail",
                "title": "Homework 1 reminder",
                "provider": "gmail",
                "status": "Unread",
            }
        ],
    }


async def check_ui(base_url, screenshots):
    page_data = fixture()
    errors, requests = [], []
    fail_next = False

    async def route_handler(route):
        nonlocal fail_next
        request = route.request
        parsed = urlsplit(request.url)
        if not request.url.startswith(base_url + "/"):
            errors.append("Unexpected external request: " + parsed.hostname)
            await route.abort()
            return
        if not parsed.path.startswith("/api/"):
            await route.continue_()
            return
        assert parsed.path == "/api/associations/task/task1"
        if request.method == "POST":
            body = request.post_data_json
            requests.append(body)
            assert request.headers.get("x-coursedeck") == "1"
            if fail_next:
                fail_next = False
                await route.fulfill(status=503, json={"detail": "Fixture update failed."})
                return
            relation = next(
                (
                    item
                    for item in page_data["relations"]
                    if item["target"]["id"] == body["target_id"]
                ),
                None,
            )
            if body["action"] == "link" and relation is None:
                relation = {
                    "id": "r3",
                    "state": "confirmed",
                    "active": True,
                    "target": page_data["available"][0],
                    "evidence": [{"kind": "manual", "label": "Linked by you"}],
                    "history": [],
                }
                page_data["relations"].append(relation)
            assert relation is not None
            relation["state"] = "confirmed" if body["action"] in {"link", "confirm"} else "rejected"
            relation["active"] = relation["state"] == "confirmed"
            relation["history"].append(
                {"action": body["action"], "created_at": "2026-09-10T12:00:00Z"}
            )
        await route.fulfill(json=page_data)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 950})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/*", route_handler)
            await page.goto(base_url + "/association-ui.html")
            await expect(page.get_by_text("Suggested · Needs confirmation")).to_be_visible()
            await expect(page.locator('.task-relations a[href^="javascript:"]')).to_have_count(0)
            await expect(page.get_by_role("link", name="Open source")).to_have_attribute(
                "href", "https://example.test/assignment/1"
            )
            task_row = page.locator(".task-relations-list > li").first
            await task_row.get_by_text("Evidence", exact=True).click()
            await expect(task_row).to_contain_text("Same course and homework:1")
            # Failed confirmation leaves the candidate visible, with a retryable action.
            fail_next = True
            await page.get_by_role("button", name="Confirm", exact=True).click()
            await expect(page.get_by_role("alert")).to_have_text("Fixture update failed.")
            await expect(page.get_by_role("button", name="Confirm", exact=True)).to_be_enabled()
            await page.get_by_role("button", name="Confirm", exact=True).click()
            await expect(task_row.get_by_role("button", name="Unlink")).to_be_visible()
            await expect(page.get_by_role("alert")).to_have_count(0)
            await page.get_by_role("button", name="Homework 1", exact=True).click()
            assert await page.evaluate("window.opened") == "task2"
            await page.get_by_role("button", name="Homework instructions", exact=True).click()
            assert await page.evaluate("window.opened") == "doc"
            await expect(page.locator(".task-relations-list > li").nth(1)).to_contain_text(
                "Incomplete"
            )
            await task_row.get_by_role("button", name="Unlink", exact=True).click()
            await expect(page.get_by_role("button", name="Homework 1", exact=True)).to_have_count(0)
            await page.get_by_label("Show dismissed links and history").check()
            await expect(task_row).to_contain_text("Dismissed link")
            await page.get_by_role("button", name="Restore", exact=True).click()
            await expect(task_row.get_by_role("button", name="Unlink")).to_be_visible()
            await page.get_by_role("button", name="Link item", exact=True).click()
            await page.get_by_label("Find an item to link", exact=True).fill("reminder")
            await page.get_by_label("Item to link", exact=True).select_option(
                json.dumps(["mail", "mail"], separators=(",", ":"))
            )
            await page.get_by_role("button", name="Link", exact=True).click()
            await page.get_by_role("button", name="Homework 1 reminder", exact=True).click()
            assert await page.evaluate("window.opened") == "mail"
            assert [item["action"] for item in requests] == [
                "confirm",
                "confirm",
                "unlink",
                "link",
                "link",
            ]
            await page.screenshot(
                path=str(screenshots / "associations-desktop.png"), full_page=True
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(screenshots / "associations-mobile.png"), full_page=True)
            assert not errors, errors
        finally:
            await browser.close()


def main():
    frontend = ROOT / "frontend"
    esbuild = frontend / "node_modules/.bin" / ("esbuild.cmd" if os.name == "nt" else "esbuild")
    if not esbuild.exists():
        raise SystemExit("Install frontend dependencies first: npm --prefix frontend ci")
    screenshots = ROOT / "data/debug"
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="associations-smoke-") as temporary:
        directory = Path(temporary)
        # stdin resolves imports relative to frontend; no source files are created or changed.
        subprocess.run(
            [
                str(esbuild),
                "--bundle",
                "--jsx=automatic",
                "--loader=tsx",
                "--sourcefile=association-smoke.tsx",
                "--outfile=" + str(directory / "association-ui.js"),
            ],
            input=HARNESS,
            text=True,
            cwd=frontend,
            check=True,
            timeout=60,
        )
        (directory / "association-ui.html").write_text(HTML, encoding="utf-8")
        handler = partial(QuietHandler, directory=str(directory))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(check_ui(f"http://127.0.0.1:{server.server_port}", screenshots))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(
        "Associations UI smoke passed: evidence, confirm failure/retry, unlink, restore, "
        "manual mail link, task/mail/document callbacks, safe URLs, 390px layout, no JS errors."
    )


if __name__ == "__main__":
    main()
