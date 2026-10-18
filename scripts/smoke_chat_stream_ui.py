"""Isolated UI stream smoke: no model requests or access to source profiles."""

import asyncio
import json
import os
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright, expect


class Handler(SimpleHTTPRequestHandler):
    conversations = {}
    gates = {}

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cid = body.get("conversation_id") or f"stream-{len(self.conversations) + 1}"
        gate = self.gates.setdefault(cid, threading.Event())
        message = {"id": str(len(self.conversations)), "role": "user", "content": body["message"]}
        conversation = self.conversations.setdefault(
            cid, {"id": cid, "title": body["message"], "course_id": None, "messages": []}
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
            text = "第一段内容"
            if body["message"] == "Parallel A":
                text += "\n\n" + "\n\n".join(
                    f"Section {index}: lecture evidence." for index in range(80)
                )
            emit({"type": "delta", "text": text})
            # The browser test must observe the first delta before releasing the rest.
            gate.wait(30)
            if body["message"] == "Parallel failure":
                emit({"type": "error", "message": "Fixture model failed", "conversation_id": cid})
                return
            step = {"id": "read", "label": "Reading a related link", "status": "done"}
            emit({"type": "activity", **step})
            emit({"type": "delta", "text": "，第二段完成。"})
            answer = {
                "id": "answer",
                "role": "assistant",
                "content": text + "，第二段完成。",
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
                cid = path.rsplit("/", 1)[-1]
                if route.request.method == "DELETE":
                    Handler.conversations.pop(cid, None)
                    result = {"id": cid}
                else:
                    result = Handler.conversations.get(cid, {})
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
        composer = page.get_by_label("Message", exact=True)
        await composer.fill("Parallel A")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-stream-text")).to_contain_text("Section 79")
        await expect(
            page.get_by_role("button", name="Delete Parallel A", exact=True)
        ).to_be_disabled()
        await composer.fill("Draft for A")
        await page.get_by_role("button", name="New chat", exact=True).click()
        await expect(composer).to_have_value("")
        await composer.fill("Parallel failure")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-stream-text")).to_have_text("第一段内容")
        await composer.fill("Draft for B")
        await expect(page.get_by_label("Generating", exact=True)).to_have_count(2)
        await page.locator(".chat-history-select").filter(has_text="Parallel A").click()
        await expect(composer).to_have_value("Draft for A")
        await expect(page.locator(".chat-stream-text")).to_contain_text("Section 79")
        Handler.gates["stream-2"].set()
        await expect(page.get_by_label("Generating", exact=True)).to_have_count(1)
        await expect(page.locator(".chat-feedback")).not_to_contain_text("Fixture model failed")
        log = page.get_by_role("log", name="Messages", exact=True)
        await log.evaluate("element => { element.scrollTop = 0; }")
        await expect(page.get_by_role("button", name="Latest", exact=True)).to_be_visible()
        Handler.gates["stream-1"].set()
        await expect(page.get_by_role("button", name="Stop", exact=True)).to_have_count(0)
        assert await log.evaluate("element => element.scrollTop < 20"), (
            "Reply stole upward scroll position"
        )
        await page.get_by_role("button", name="Latest", exact=True).click()
        assert await log.evaluate(
            "element => element.scrollHeight - element.scrollTop - element.clientHeight < 64"
        )
        await page.locator(".chat-history-select").filter(has_text="Parallel failure").click()
        await expect(page.locator(".chat-feedback")).to_contain_text("Fixture model failed")
        await expect(composer).to_have_value("Draft for B")
        await expect(page.locator(".chat-message.user")).to_have_count(1)
        await page.get_by_role("button", name="New chat", exact=True).click()
        await composer.fill("Background completion")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-stream-text")).to_have_text("第一段内容")
        await page.locator("nav").get_by_role("button", name=re.compile(r"^Todo")).click()
        Handler.gates["stream-3"].set()
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        await expect(page.locator(".chat-message.assistant .chat-message-content")).to_have_text(
            "第一段内容，第二段完成。"
        )
        await expect(page.get_by_role("button", name="Stop", exact=True)).to_have_count(0)
        await page.get_by_role("button", name="New chat", exact=True).click()
        await composer.fill("Stop this reply")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-stream-text")).to_have_text("第一段内容")
        await page.get_by_role("button", name="Stop", exact=True).click()
        await expect(page.locator(".chat-feedback")).to_contain_text("Stopped.")
        await expect(page.locator(".chat-message.assistant").last).to_contain_text("第一段内容")
        page.on("dialog", lambda dialog: dialog.accept())
        await page.get_by_role("button", name="Delete Parallel failure", exact=True).click()
        await expect(
            page.locator(".chat-history-select").filter(has_text="Parallel failure")
        ).to_have_count(0)
        await expect(page.locator(".chat-feedback")).to_contain_text("Stopped.")
        await page.get_by_role("button", name="Delete Stop this reply", exact=True).click()
        await expect(page.locator(".chat-message")).to_have_count(0)
        await expect(page.locator(".chat-feedback")).not_to_contain_text("Stopped.")
        await page.locator(".chat-history-select").filter(has_text="Parallel A").click()
        await expect(composer).to_have_value("Draft for A")
        await expect(page.locator(".chat-message.assistant")).to_contain_text("Section 79")
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        Path("data/debug").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path="data/debug/chat-stream-mobile.png", full_page=True)
        assert not errors, errors
        for gate in Handler.gates.values():
            gate.set()
        await browser.close()


def main():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(
            Handler,
            directory=str(Path(os.environ.get("COURSEDECK_UI_DIST", "frontend/dist")).resolve()),
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
    finally:
        for gate in Handler.gates.values():
            gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(
        "Chat stream UI smoke passed: two simultaneous streams, session drafts and errors, "
        "background navigation, history cache, stop, deletion, scroll position and mobile."
    )


if __name__ == "__main__":
    main()
