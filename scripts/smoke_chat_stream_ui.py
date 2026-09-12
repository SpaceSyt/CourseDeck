"""Isolated UI stream smoke: no model requests or access to source profiles."""

import asyncio
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright, expect


class Handler(SimpleHTTPRequestHandler):
    release = threading.Event()
    conversations = {}

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cid = "stream-chat"
        message = {"id": str(len(self.conversations)), "role": "user", "content": body["message"]}
        conversation = self.conversations.setdefault(
            cid, {"id": cid, "title": "Stream test", "course_id": None, "messages": []}
        )
        conversation["messages"].append(message)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event):
            self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            emit({"type": "session", "conversation_id": cid, "message": message})
            emit(
                {
                    "type": "activity",
                    "id": "read",
                    "label": "Reading a related link",
                    "status": "running",
                }
            )
            emit({"type": "answer_start"})
            emit({"type": "delta", "text": "第一段内容"})
            # The browser test must observe the first delta before releasing the rest.
            self.release.wait(10)
            step = {"id": "read", "label": "Reading a related link", "status": "done"}
            emit({"type": "activity", **step})
            emit({"type": "delta", "text": "，第二段完成。"})
            answer = {
                "id": "answer",
                "role": "assistant",
                "content": "第一段内容，第二段完成。",
                "activity": [step],
            }
            conversation["messages"].append(answer)
            emit({"type": "done", "conversation_id": cid, "message": answer, "warnings": []})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


async def check(base):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def api(route):
            path = route.request.url.split("/api/")[-1]
            if path == "chat/messages/stream":
                await route.continue_()
                return
            result = {}
            if path == "snapshot":
                result = {
                    "courses": [],
                    "source_courses": [],
                    "tasks": [],
                    "sources": [],
                    "settings": {"timezone": "America/New_York", "show_completed": False},
                    "revision": 1,
                }
            elif path == "heartbeat":
                result = {"service": "coursedeck", "status": "connected"}
            elif path == "chat/config":
                result = {
                    "model": "fixture",
                    "base_url": "https://example.test",
                    "has_api_key": False,
                }
            elif path == "chat/conversations":
                result = {"conversations": list(Handler.conversations.values())}
            elif path.startswith("chat/conversations/"):
                result = Handler.conversations.get(path.rsplit("/", 1)[-1], {})
            elif path.startswith("mail"):
                result = {
                    "messages": [],
                    "total": 0,
                    "has_more": False,
                    "connection": {"status": "disconnected"},
                }
            await route.fulfill(content_type="application/json", body=json.dumps(result))

        await page.route("**/api/**", api)
        await page.goto(base)
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        await page.get_by_label("Message", exact=True).fill("Find assignment links")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-stream-text")).to_have_text("第一段内容")
        await expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
        await page.locator(".chat-activity summary").click()
        await expect(page.locator(".chat-activity li")).to_contain_text("Reading a related link")
        Handler.release.set()
        await expect(page.get_by_role("button", name="Stop", exact=True)).to_have_count(0)
        await expect(page.locator(".chat-message.assistant .chat-message-content")).to_have_text(
            "第一段内容，第二段完成。"
        )
        await page.locator(".chat-activity summary").click()
        await expect(page.locator(".chat-activity li")).to_contain_text("done")
        await page.get_by_role("button", name="New chat", exact=True).click()
        await page.locator(".chat-history-list button").first.click()
        await expect(page.locator(".chat-activity")).to_have_count(1)
        Handler.release.clear()
        await page.get_by_label("Message", exact=True).fill("Read another link")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-stream-text")).to_have_text("第一段内容")
        await page.get_by_role("button", name="Stop", exact=True).click()
        await expect(page.locator(".chat-feedback")).to_contain_text("Stopped.")
        await expect(page.locator(".chat-message.assistant").last).to_contain_text("第一段内容")
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        Path("data/debug").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path="data/debug/chat-stream-mobile.png", full_page=True)
        assert not errors, errors
        Handler.release.set()
        await browser.close()


def main():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(Path("frontend/dist").resolve()))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
    finally:
        Handler.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(
        "Chat stream UI smoke passed: incremental text, expandable tools, history, stop and mobile."
    )


if __name__ == "__main__":
    main()
