"""Exercise memory management with local fixtures and no model requests."""

import asyncio
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.async_api import async_playwright, expect


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


async def check(base):
    course = {"id": "course-1", "name": "Programming", "provider": "brightspace"}
    memories = [
        {
            "id": "global",
            "text": "Use concise answers",
            "course_id": None,
            "version": "v1",
            "status": "active",
            "origin": "manual",
        },
        {
            "id": "pending",
            "text": "Start revisions one week ahead",
            "course_id": "course-1",
            "version": "v2",
            "status": "pending",
            "origin": "chat",
            "source_quote": "I may want to start revisions a week ahead.",
        },
    ]
    mode = "automatic"
    conflict = True
    reads = 0
    writes = []
    errors = []

    async def route_api(route):
        nonlocal mode, conflict, reads
        request = route.request
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        status = 200
        result = {}
        if path == "/api/snapshot":
            result = {
                "courses": [course],
                "source_courses": [course],
                "tasks": [],
                "sources": [],
                "settings": {"timezone": "America/New_York", "show_completed": False},
                "revision": 1,
            }
        elif path == "/api/heartbeat":
            result = {"service": "coursedeck", "status": "connected"}
        elif path == "/api/chat/config":
            result = {"model": "", "base_url": "", "has_api_key": False}
        elif path == "/api/chat/conversations":
            result = {"conversations": []}
        elif path.startswith("/api/mail"):
            result = {
                "messages": [],
                "total": 0,
                "has_more": False,
                "connection": {"status": "disconnected"},
            }
        elif path == "/api/chat/memories/export":
            await route.fulfill(
                content_type="text/markdown", body="# Chat memories\n\nUse examples\n"
            )
            return
        elif path == "/api/chat/memories/settings":
            mode = request.post_data_json["mode"]
            result = {"mode": mode}
        elif path == "/api/chat/memories":
            if request.method == "POST":
                body = request.post_data_json
                writes.append(body)
                result = {
                    "id": "added",
                    **body,
                    "version": "v3",
                    "status": "active",
                    "origin": "manual",
                }
                memories.append(result)
            else:
                reads += 1
                result = {"mode": mode, "memories": memories}
        elif path.startswith("/api/chat/memories/"):
            memory_id = path.split("/")[4]
            memory = next(item for item in memories if item["id"] == memory_id)
            if request.method == "DELETE":
                assert parse_qs(parsed.query)["version"][0] == memory["version"]
                memories.remove(memory)
                result = {"id": memory_id}
            else:
                body = request.post_data_json
                assert body["expected_version"] == memory["version"]
                if path.endswith("/approve"):
                    memory.update(status="active", version="approved")
                    result = memory
                elif memory_id == "global" and conflict:
                    conflict = False
                    memory.update(text="Use examples", version="concurrent")
                    status = 409
                    result = {"detail": "Memory changed; reload before editing."}
                else:
                    memory.update(text=body["text"], course_id=body["course_id"], version="edited")
                    result = memory
        else:
            errors.append(f"Unexpected API request: {path}")
            status = 404
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(result))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/**", route_api)
        await page.goto(base)
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        await page.get_by_role("button", name="Memories", exact=True).click()
        panel = page.get_by_role("region", name="Memories", exact=True)
        await expect(panel.get_by_label("Memory mode", exact=True)).to_have_value("automatic")
        await expect(panel.get_by_role("article")).to_have_count(2)
        pending = panel.get_by_role("article", name="Start revisions one week ahead", exact=True)
        await expect(pending).to_contain_text("Needs approval")
        await expect(pending).to_contain_text("Programming")
        await pending.locator("summary").click()
        await expect(pending.locator("blockquote")).to_have_text(
            "I may want to start revisions a week ahead."
        )
        await pending.get_by_role("button", name="Approve", exact=True).click()
        await expect(pending).not_to_contain_text("Needs approval")
        await panel.get_by_label("Memory mode", exact=True).select_option("confirm")
        await expect(panel.get_by_label("Memory mode", exact=True)).to_have_value("confirm")
        await panel.get_by_role("button", name="Add memory", exact=True).click()
        await panel.get_by_label("Memory text", exact=True).fill("Use Python examples")
        await panel.get_by_label("Memory scope", exact=True).select_option("course-1")
        await panel.get_by_role("button", name="Add", exact=True).click()
        added = panel.get_by_role("article", name="Use Python examples", exact=True)
        await expect(added).to_contain_text("Programming")
        assert writes == [{"text": "Use Python examples", "course_id": "course-1"}]
        global_memory = panel.get_by_role("article", name="Use concise answers", exact=True)
        await global_memory.get_by_role("button", name="Edit", exact=True).click()
        await panel.get_by_label("Memory text", exact=True).fill("My unsaved draft")
        await panel.get_by_role("button", name="Save", exact=True).click()
        await expect(panel.get_by_label("Memory text", exact=True)).to_have_value(
            "My unsaved draft"
        )
        await expect(panel.get_by_role("button", name="Save", exact=True)).to_be_disabled()
        await expect(panel.get_by_role("article", name="Use examples", exact=True)).to_be_visible()
        await panel.get_by_role("button", name="Load latest", exact=True).click()
        await expect(panel.get_by_label("Memory text", exact=True)).to_have_value("Use examples")
        await panel.get_by_label("Memory text", exact=True).fill("Use short examples")
        await panel.get_by_role("button", name="Save", exact=True).click()
        await expect(
            panel.get_by_role("article", name="Use short examples", exact=True)
        ).to_be_visible()
        page.on("dialog", lambda dialog: dialog.accept())
        await added.get_by_role("button", name="Delete", exact=True).click()
        await expect(added).to_have_count(0)
        async with page.expect_download() as downloaded:
            await panel.get_by_role("button", name="Export", exact=True).click()
        download = await downloaded.value
        assert download.suggested_filename == "coursedeck-memories.md"
        assert "# Chat memories" in Path(await download.path()).read_text(encoding="utf-8")
        previous_reads = reads
        await page.get_by_role("button", name="Conversation", exact=True).click()
        await page.get_by_role("button", name="Memories", exact=True).click()
        await expect(panel.get_by_role("article")).to_have_count(2)
        assert reads > previous_reads
        Path("data/debug").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path="data/debug/chat-memories-desktop.png", full_page=True)
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=f"data/debug/chat-memories-{width}.png", full_page=True)
        assert not errors, errors
        await browser.close()


def main():
    handler = partial(
        Handler,
        directory=str(Path(os.environ.get("COURSEDECK_UI_DIST", "frontend/dist")).resolve()),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    print(
        "Memory UI smoke passed: mode, scopes, CRUD, pending approval, "
        "version conflict, export, re-entry and mobile."
    )


if __name__ == "__main__":
    main()
