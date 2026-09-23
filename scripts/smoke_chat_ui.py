"""Exercise Chat in a real browser with synthetic, fully local API responses."""

import asyncio
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.async_api import async_playwright, expect


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def check_ui(base_url):
    course = {"id": "course-1", "name": "Programming fixture", "provider": "brightspace"}
    snapshot = {
        "courses": [course],
        "source_courses": [course],
        "tasks": [],
        "sources": [],
        "settings": {
            "timezone": "America/New_York",
            "startup_sync": False,
            "show_completed": False,
        },
        "revision": 1,
    }
    config = {
        "base_url": "https://api.example.test/v1",
        "model": "fixture-model",
        "has_api_key": False,
        "builtin_prompt": "Use local evidence and cite it.",
    }
    document = {
        "id": "material:loops",
        "title": "Loops lecture",
        "provider": "brightspace",
        "course_id": course["id"],
        "kind": "lecture",
        "body": "Cached lecture excerpt",
        "body_truncated": True,
        "url": "https://example.test/lecture",
        "complete": True,
        "fetched_at": "2026-09-10T12:00:00+00:00",
        "checked_at": "2026-09-10T12:00:00+00:00",
    }
    missing_warning = "Lecture 1: text could not be read (HTTP 404)."
    cached_warning = "Lecture 2: refresh failed; earlier cached text retained."

    def library_document(index):
        item = document | {"id": f"material-{index}", "title": f"Lecture {index}"}
        if index == 1:
            item.update(
                body="",
                complete=False,
                warnings=[missing_warning],
                fetched_at=None,
                checked_at="2026-09-10T13:00:00+00:00",
                updated_at="2026-08-01T09:00:00+00:00",
            )
        elif index == 2:
            item.update(
                body="Earlier cached lecture text.",
                complete=False,
                warnings=[cached_warning],
                fetched_at="2026-09-09T12:00:00+00:00",
                checked_at="2026-09-10T13:00:00+00:00",
                source_modified_at="2026-09-08T08:00:00+00:00",
            )
        return item

    conversations = {}
    config_writes = []
    message_writes = []
    warnings = ["Brightspace: one document could not be read."]

    async def route_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        result = {}
        status = 200
        if path == "/api/snapshot":
            result = snapshot
        elif path == "/api/heartbeat":
            result = {"service": "coursedeck", "status": "connected"}
        elif path == "/api/mail":
            result = {
                "messages": [],
                "total": 0,
                "has_more": False,
                "connection": {"status": "disconnected", "syncing": False},
            }
        elif path == "/api/chat/config":
            if request.method == "PUT":
                body = request.post_data_json
                config_writes.append(body)
                if body["base_url"] != config["base_url"] or body.get("clear_api_key"):
                    config["has_api_key"] = False
                if body.get("api_key"):
                    config["has_api_key"] = True
                config.update({key: body[key] for key in ("base_url", "model")})
            result = config
        elif path == "/api/chat/conversations":
            result = {
                "conversations": [
                    {key: value for key, value in item.items() if key != "messages"}
                    for item in conversations.values()
                ]
            }
        elif "/citations/" in path:
            assert path.endswith("mail:fixture"), path
            result = document | {
                "id": "mail:fixture",
                "kind": "email",
                "provider": "gmail",
                "title": "Email fixture",
                "body": "Verified email body from the conversation source.",
                "body_truncated": False,
            }
        elif path.startswith("/api/chat/conversations/"):
            result = conversations[path.rsplit("/", 1)[1]]
        elif path == "/api/chat/messages/stream":
            body = request.post_data_json
            message_writes.append(body)
            cid = body.get("conversation_id") or f"conversation-{len(conversations) + 1}"
            conversation = conversations.setdefault(
                cid,
                {
                    "id": cid,
                    "title": body["message"],
                    "course_id": body.get("course_id"),
                    "messages": [],
                },
            )
            conversation["messages"].append(
                {
                    "id": f"user-{len(message_writes)}",
                    "role": "user",
                    "content": body["message"],
                    "citations": [],
                }
            )
            if body["message"] == "Trigger failure":
                status = 502
                result = {
                    "detail": {
                        "message": "Model unavailable; your message is saved.",
                        "conversation_id": cid,
                    }
                }
            else:
                assistant = {
                    "id": f"reply-{len(message_writes)}",
                    "role": "assistant",
                    "content": "## Review loops\n\n"
                    "| Task | Due | Source |\n| --- | --- | --- |\n"
                    "| **Loop homework** | Friday 23:59 | [[material:loops]] |\n\n"
                    "1. Read the notes\n2. Check `range(3)`\n\n"
                    "```python\nfor i in range(3):\n    print(i)\n```\n\n"
                    "[[unknown-reference]] <script>unsafe()</script>",
                    "citations": [
                        document,
                        document
                        | {
                            "id": "mail:fixture",
                            "title": "Email fixture",
                            "kind": "email",
                            "provider": "gmail",
                            "url": None,
                        },
                        document
                        | {"id": "unsafe", "title": "Unsafe URL", "url": "javascript:alert(1)"},
                    ],
                    "warnings": warnings,
                }
                if body["message"] == "Show a long explanation":
                    assistant["content"] += "\n\n" + "\n".join(
                        f"Lecture section {index}: examine the next loop iteration."
                        for index in range(100)
                    )
                conversation["messages"].append(assistant)
                result = {"conversation_id": cid, "message": assistant, "warnings": warnings}
                stream = [
                    {
                        "type": "session",
                        "conversation_id": cid,
                        "message": conversation["messages"][-2],
                    },
                    {"type": "answer_start"},
                    {"type": "delta", "text": assistant["content"]},
                    {"type": "done", **result},
                ]
                await route.fulfill(
                    content_type="text/event-stream",
                    body="".join("data: " + json.dumps(item) + "\n\n" for item in stream),
                )
                return
        elif path == "/api/chat/library":
            offset = int(query.get("offset", ["0"])[0])
            empty = query.get("query", [""])[0] == "nothing-matches"
            documents = (
                []
                if empty
                else [library_document(index) for index in range(offset, min(offset + 50, 51))]
            )
            result = {
                "documents": documents,
                "total": 0 if empty else 51,
                "warnings": warnings + [missing_warning, cached_warning],
            }
        elif path.startswith("/api/chat/library/"):
            result = document | {
                "body": "Verified full document. All lecture sections are here.",
                "body_truncated": False,
            }
            if path.rsplit("/", 1)[1] in {"material-1", "material-2"}:
                result = library_document(int(path.rsplit("-", 1)[1])) | {"body_truncated": False}
        else:
            status = 404
            result = {"detail": f"Unexpected mock endpoint: {path}"}
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(result))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/**", route_api)
        await page.goto(base_url)
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        workspace = await page.locator(".chat-workspace").bounding_box()
        history = await page.locator(".chat-history").bounding_box()
        chat_main = await page.locator(".chat-main").bounding_box()
        assert workspace and history and chat_main
        assert abs(history["x"] - workspace["x"]) <= 2
        assert history["x"] + history["width"] <= chat_main["x"] + 1
        composer = page.locator("form.chat-composer")
        send_button = composer.get_by_role("button", name="Send", exact=True)
        find_materials = composer.get_by_role("button", name="Refresh materials", exact=True)
        await expect(send_button).to_be_disabled()
        await expect(composer.get_by_label("Message", exact=True)).to_have_count(1)
        await expect(find_materials).to_have_attribute("aria-pressed", "false")
        await expect(find_materials).to_have_attribute("type", "button")
        await page.get_by_label("Message", exact=True).fill("   ")
        await expect(send_button).to_be_disabled()
        await page.get_by_label("Message", exact=True).fill("")
        await page.get_by_role("button", name="Materials", exact=True).click()
        await expect(page.locator(".material-list button")).to_have_count(50)
        await page.get_by_label("Chat course", exact=True).select_option(course["id"])
        await expect(page.get_by_role("button", name="Materials", exact=True)).to_have_attribute(
            "aria-pressed", "true"
        )
        await expect(page.locator(".material-list button")).to_have_count(50)
        await expect(page.locator(".material-library")).to_contain_text(warnings[0])
        await expect(page.locator(".material-list button").nth(1)).to_contain_text(
            "Text unavailable"
        )
        await expect(page.locator(".material-list button").nth(2)).to_contain_text("Incomplete")
        await page.locator(".material-list button").nth(1).click()
        await expect(page.locator(".material-detail")).to_contain_text(missing_warning)
        await expect(page.get_by_text(missing_warning, exact=True)).to_have_count(1)
        missing_times = page.get_by_label("Material retrieval times", exact=True)
        await expect(missing_times).to_contain_text("Checked")
        await expect(missing_times).not_to_contain_text("Fetched")
        await expect(missing_times.locator("time")).to_have_attribute(
            "datetime", "2026-09-10T13:00:00.000Z"
        )
        await expect(page.locator(".material-detail")).to_contain_text("No text extracted.")
        await page.get_by_role("button", name="Back to materials", exact=True).click()
        await page.locator(".material-list button").nth(2).click()
        await expect(page.locator(".material-detail")).to_contain_text(cached_warning)
        await expect(page.get_by_text(cached_warning, exact=True)).to_have_count(1)
        await expect(page.locator(".material-body")).to_have_text("Earlier cached lecture text.")
        cached_times = page.get_by_label("Material retrieval times", exact=True)
        await expect(cached_times).to_contain_text("Source updated")
        await expect(cached_times).to_contain_text("Read")
        await expect(cached_times).to_contain_text("Checked")
        await expect(cached_times.locator("time").nth(0)).to_have_attribute(
            "datetime", "2026-09-08T08:00:00.000Z"
        )
        await expect(cached_times.locator("time").nth(1)).to_have_attribute(
            "datetime", "2026-09-09T12:00:00.000Z"
        )
        await expect(cached_times.locator("time").nth(2)).to_have_attribute(
            "datetime", "2026-09-10T13:00:00.000Z"
        )
        await page.get_by_role("button", name="Back to materials", exact=True).click()
        await page.get_by_role("button", name="Load more", exact=True).click()
        await expect(page.locator(".material-list button")).to_have_count(51)
        await page.get_by_label("Search materials", exact=True).fill("nothing-matches")
        await expect(page.get_by_text("No materials found.", exact=True)).to_be_visible()
        await page.get_by_label("Search materials", exact=True).fill("")
        await expect(page.locator(".material-list button")).to_have_count(50)
        await page.locator(".material-list button").first.click()
        await expect(page.locator(".material-body")).to_contain_text("All lecture sections")
        assert config["has_api_key"] is False
        await page.get_by_role("button", name="Conversation", exact=True).click()
        await page.get_by_label("Chat course", exact=True).select_option(course["id"])
        await page.get_by_label("Message", exact=True).fill("Explain loops")
        await find_materials.click()
        await expect(find_materials).to_have_attribute("aria-pressed", "true")
        assert not message_writes, "Changing the material mode must not submit the draft."
        await send_button.click()
        await expect(page.locator(".chat-message.assistant")).to_have_count(1)
        await expect(send_button).to_be_disabled()
        assert message_writes[-1]["fetch_materials"] is True
        assert message_writes[-1]["course_id"] == course["id"]
        await expect(page.locator(".chat-message.assistant")).to_contain_text(warnings[0])
        await expect(page.locator(".chat-missing-citation")).to_contain_text("source unavailable")
        assert await page.locator(".chat-message script").count() == 0
        assert await page.locator('.chat-citations a[href^="javascript:"]').count() == 0
        await expect(page.locator(".chat-markdown h2")).to_have_text("Review loops")
        await expect(page.locator(".chat-markdown table tbody tr")).to_have_count(1)
        await expect(page.locator(".chat-markdown td strong")).to_have_text("Loop homework")
        await expect(page.locator(".chat-markdown pre code")).to_contain_text("print(i)")
        await page.get_by_role("button", name="[2] Email fixture", exact=True).click()
        await expect(page.locator(".material-body")).to_contain_text("Verified email body")
        await page.get_by_role("button", name="Conversation", exact=True).click()
        await page.locator(".chat-inline-citation").click()
        await expect(page.locator(".material-body")).to_contain_text("All lecture sections")
        await page.get_by_role("button", name="Conversation", exact=True).click()
        await page.get_by_role("button", name="New chat", exact=True).click()
        await page.locator(".chat-history-list button").first.click()
        await expect(page.locator(".chat-message.assistant")).to_contain_text(warnings[0])
        Path("data/debug").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path="data/debug/chat-desktop.png", full_page=True)
        await page.get_by_label("Message", exact=True).fill("Show a long explanation")
        await find_materials.click()
        await expect(find_materials).to_have_attribute("aria-pressed", "false")
        assert len(message_writes) == 1
        await send_button.click()
        await expect(page.locator(".chat-message.assistant")).to_have_count(2)
        await expect(page.locator(".chat-message.assistant").last).to_contain_text(
            "Lecture section 99"
        )
        assert message_writes[-1]["fetch_materials"] is False
        assert message_writes[-1]["conversation_id"] == message_writes[0].get(
            "conversation_id", "conversation-1"
        )
        message_log = page.get_by_role("log", name="Messages", exact=True)
        assert await message_log.evaluate("element => element.scrollHeight > element.clientHeight")
        await message_log.evaluate("element => { element.scrollTop = 0; }")
        composer_box = await composer.bounding_box()
        assert composer_box and composer_box["y"] >= 0
        assert composer_box["y"] + composer_box["height"] <= 1081
        assert await composer.evaluate("""element => {
            const frame = element.getBoundingClientRect();
            return [...element.querySelectorAll('textarea, button')].every(control => {
                const box = control.getBoundingClientRect();
                return box.left >= frame.left && box.right <= frame.right
                    && box.top >= frame.top && box.bottom <= frame.bottom;
            });
        }""")
        await page.get_by_role("button", name="New chat", exact=True).click()
        await page.get_by_label("Message", exact=True).fill("Trigger failure")
        await page.get_by_role("button", name="Send", exact=True).click()
        await expect(page.locator(".chat-feedback")).to_contain_text("your message is saved")
        await expect(page.locator(".chat-message.user")).to_have_count(1)
        await expect(page.get_by_label("Message", exact=True)).to_have_value("")
        await page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
        settings = page.locator(".ai-settings")
        await settings.get_by_label("API key", exact=True).fill("fixture-key-for-ui-test")
        await settings.get_by_role("button", name="Save", exact=True).click()
        await expect(settings.get_by_label("API key", exact=True)).to_have_value("")
        assert config_writes[-1]["api_key"] == "fixture-key-for-ui-test"
        await settings.get_by_label("Model", exact=True).fill("another-fixture-model")
        await settings.get_by_role("button", name="Save", exact=True).click()
        await expect(settings.get_by_role("button", name="Save", exact=True)).to_be_disabled()
        assert "api_key" not in config_writes[-1]
        await settings.get_by_label("Remove saved key", exact=True).check()
        await settings.get_by_role("button", name="Save", exact=True).click()
        await expect(settings.get_by_label("Remove saved key", exact=True)).to_have_count(0)
        assert config_writes[-1]["clear_api_key"] is True
        config["credential_error"] = "Credential store unavailable."
        await page.locator("nav").get_by_role("button", name="Chat", exact=True).click()
        await expect(page.locator(".chat-feedback")).to_contain_text(config["credential_error"])
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator(".chat-history-list button").first.click()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path="data/debug/chat-mobile.png", full_page=True)
        await page.get_by_role("button", name="Materials", exact=True).click()
        await expect(page.locator(".material-list button")).to_have_count(50)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path="data/debug/materials-mobile.png", full_page=True)
        await page.set_viewport_size({"width": 320, "height": 740})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.get_by_role("button", name="Conversation", exact=True).click()
        await expect(page.locator(".chat-message.assistant")).to_have_count(2)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        history = await page.locator(".chat-history").bounding_box()
        chat_main = await page.locator(".chat-main").bounding_box()
        assert history and chat_main
        assert history["y"] + history["height"] <= chat_main["y"] + 1
        await expect(send_button).to_be_visible()
        await expect(find_materials).to_be_visible()
        await page.screenshot(path="data/debug/chat-mobile-320.png", full_page=True)
        assert not errors, errors
        await browser.close()


def main():
    handler = partial(
        QuietHandler,
        directory=str(Path(os.environ.get("COURSEDECK_UI_DIST", "frontend/dist")).resolve()),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check_ui(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(
        "Chat UI smoke passed: materials without AI, citations, saved warnings, "
        "material completeness and cache timestamps, failure recovery, key controls, "
        "integrated composer, mode toggle, desktop scrolling, 390/320px mobile."
    )


if __name__ == "__main__":
    main()
