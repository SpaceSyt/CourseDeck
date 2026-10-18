"""Exercise file upload, session isolation and citations with synthetic local APIs."""

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
    uploads = {}
    conversations = {}
    sends = []
    deleted = []
    errors = []
    release_pdf = asyncio.Event()
    warning = "Page 2 contains no readable text. Scanned pages and images are not read."

    async def route_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        status = 200
        result = {}
        if path == "/api/snapshot":
            result = {
                "courses": [],
                "source_courses": [],
                "tasks": [],
                "sources": [],
                "settings": {"timezone": "America/New_York", "show_completed": False},
                "revision": 1,
            }
        elif path == "/api/heartbeat":
            result = {"service": "coursedeck", "status": "connected"}
        elif path == "/api/chat/config":
            result = {"model": "fixture", "base_url": "", "has_api_key": True}
        elif path == "/api/chat/conversations":
            result = {"conversations": list(conversations.values())}
        elif path.startswith("/api/mail"):
            result = {
                "messages": [],
                "total": 0,
                "has_more": False,
                "connection": {"status": "disconnected"},
            }
        elif path == "/api/chat/attachments" and request.method == "POST":
            name = parse_qs(parsed.query)["filename"][0]
            assert request.headers["content-type"] == "application/octet-stream"
            assert request.headers["x-coursedeck"] == "1"
            if name == "locked.pdf":
                status, result = 422, {"detail": "PDF is password protected."}
            else:
                if name == "lecture.pdf":
                    await release_pdf.wait()
                aid = f"upload-{len(uploads) + 1}"
                pdf = name.endswith(".pdf")
                result = {
                    "id": aid,
                    "name": name,
                    "size": len(request.post_data_buffer),
                    "media_type": "application/pdf" if pdf else "text/plain",
                    "pages": 2 if pdf else None,
                    "characters": 50,
                    "warnings": [warning] if pdf else [],
                    "complete": not pdf,
                }
                uploads[aid] = result
        elif path.startswith("/api/chat/attachments/") and request.method == "DELETE":
            aid = path.split("/")[4]
            deleted.append(aid)
            uploads.pop(aid)
        elif path == "/api/chat/messages/stream":
            body = request.post_data_json
            sends.append(body)
            cid = body.get("conversation_id") or "saved-files"
            conversation = conversations.setdefault(
                cid,
                {
                    "id": cid,
                    "title": "File summary",
                    "course_id": None,
                    "messages": [],
                },
            )
            attachments = [uploads[aid] for aid in body.get("attachment_ids", [])]
            user = {
                "id": f"u{len(sends)}",
                "role": "user",
                "content": body["message"],
                "attachments": attachments,
            }
            citation = {
                "id": attachments[0]["id"],
                "title": attachments[0]["name"],
                "url": None,
                "provider": "upload",
                "kind": "uploaded_file",
            }
            answer = {
                "id": f"a{len(sends)}",
                "role": "assistant",
                "content": f"The lecture discusses limits. [[{citation['id']}]]",
                "citations": [citation],
            }
            conversation["messages"].extend([user, answer])
            events = [
                {"type": "session", "conversation_id": cid, "message": user},
                {"type": "done", "conversation_id": cid, "message": answer},
            ]
            await route.fulfill(
                content_type="text/event-stream",
                body="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            )
            return
        elif "/citations/" in path:
            aid = path.rsplit("/", 1)[1]
            result = {
                "id": aid,
                "title": uploads[aid]["name"],
                "url": None,
                "provider": "upload",
                "kind": "uploaded_file",
                "course_id": None,
                "body": "Page 1\nThe lecture discusses limits.",
                "complete": False,
                "warnings": [warning],
            }
        elif path.startswith("/api/chat/conversations/"):
            result = conversations[path.rsplit("/", 1)[1]]
        elif path == "/api/chat/library":
            result = {"documents": [], "total": 0, "warnings": []}
        else:
            errors.append(f"Unexpected API request: {request.method} {path}")
            status = 404
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(result))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/**", route_api)
        await page.goto(base)
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        picker = page.locator('input[type="file"]')
        await picker.set_input_files(
            {
                "name": "lecture.pdf",
                "mimeType": "application/pdf",
                "buffer": b"%PDF-synthetic fixture",
            }
        )
        await expect(page.get_by_text("Uploading…", exact=True)).to_be_visible()
        await expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
        await page.locator(".chat-history > button").click()
        await expect(page.get_by_role("list", name="Attached files")).to_have_count(0)
        await picker.set_input_files(
            {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"Independent draft"}
        )
        await expect(page.get_by_role("link", name="notes.txt")).to_be_visible()
        release_pdf.set()
        await page.locator(".chat-history-select").first.click()
        await expect(page.get_by_role("link", name="lecture.pdf")).to_be_visible()
        await expect(page.get_by_text(warning, exact=True)).to_be_visible()
        await expect(page.get_by_text("2 pages · Text only", exact=False)).to_be_visible()
        await expect(page.get_by_role("link", name="notes.txt")).to_have_count(0)
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-message.assistant")).to_have_count(1)
        assert len(sends) == 1
        assert sends[0]["message"] == "Summarize the attached files."
        aid = sends[0]["attachment_ids"][0]
        assert uploads[aid]["name"] == "lecture.pdf"
        await expect(page.get_by_role("list", name="Message files")).to_have_count(1)
        await expect(page.get_by_role("list", name="Attached files")).to_have_count(0)
        await expect(page.get_by_role("link", name="lecture.pdf")).to_have_attribute(
            "href", f"/api/chat/attachments/{aid}/download"
        )
        await page.locator(".chat-citations button").click()
        await expect(page.locator(".material-body")).to_contain_text(
            "The lecture discusses limits."
        )
        await expect(page.get_by_role("link", name="Open file")).to_have_attribute(
            "href", f"/api/chat/attachments/{aid}/download"
        )
        await page.get_by_role("button", name="Conversation", exact=True).click()
        await page.reload()
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        await page.locator(".chat-history-select").filter(has_text="File summary").click()
        await expect(page.get_by_role("link", name="lecture.pdf")).to_be_visible()
        await picker.set_input_files(
            {"name": "locked.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-locked fixture"}
        )
        await expect(page.get_by_text("PDF is password protected.")).to_be_visible()
        await expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
        await page.get_by_role("button", name="Remove locked.pdf").click()
        await picker.set_input_files(
            {"name": "removable.txt", "mimeType": "text/plain", "buffer": b"Remove this draft"}
        )
        await expect(page.get_by_role("link", name="removable.txt")).to_be_visible()
        await page.get_by_role("button", name="Remove removable.txt").click()
        await expect(page.get_by_role("list", name="Attached files")).to_have_count(0)
        assert len(deleted) == 1
        await picker.set_input_files(
            {
                "name": "mobile-long-file-name-for-attachment-wrapping.pdf",
                "mimeType": "application/pdf",
                "buffer": b"%PDF-mobile",
            }
        )
        await expect(page.get_by_role("list", name="Attached files")).to_be_visible()
        Path("data/debug").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path="data/debug/chat-upload-desktop.png", full_page=True)
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await expect(
                page.get_by_role("button", name="Attach files", exact=True)
            ).to_be_visible()
            await expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
            await page.screenshot(path=f"data/debug/chat-upload-{width}.png", full_page=True)
        await picker.set_input_files(
            [
                {"name": f"notes-{index}.txt", "mimeType": "text/plain", "buffer": b"Draft"}
                for index in range(9)
            ]
        )
        await expect(page.get_by_role("list", name="Attached files").locator("li")).to_have_count(
            10
        )
        await expect(page.get_by_role("button", name="Attach files", exact=True)).to_be_disabled()
        await expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
        assert await page.locator(".chat-composer").evaluate(
            "element => element.getBoundingClientRect().bottom <= innerHeight"
        )
        assert await page.get_by_role("list", name="Attached files").evaluate(
            "element => element.clientHeight <= 220 && element.scrollHeight > element.clientHeight"
        )
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
        "Chat upload UI smoke passed: upload, session isolation, file-only send, history, "
        "citations, warnings, rejected files, removal and mobile."
    )


if __name__ == "__main__":
    main()
