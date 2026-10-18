"""Confirm mail deletion UI using synthetic plans; never access real mail or Gmail."""

import asyncio
import copy
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright, expect


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def fixture(plan_id, destination="local", count=1):
    return {
        "id": plan_id,
        "version": "latest-version",
        "destination": destination,
        "status": "pending",
        "matched": count,
        "missing_date_count": 0,
        "description": "Received before September 1, 2026",
        "messages": [
            {
                "id": f"{plan_id}-{index}",
                "subject": f"Fixture email {index + 1}",
                "sender": "Course sender",
                "received_at": "2026-08-01T12:00:00Z",
            }
            for index in range(count)
        ],
    }


def complete(plan):
    plan.update(
        status="done",
        version="finished",
        result={
            "changed": len(plan["messages"]),
            "failed": 0,
            "unverified": 0,
            "skipped": 0,
            "results": [{"id": item["id"], "status": "deleted"} for item in plan["messages"]],
        },
    )


async def check(base):
    plans = {
        "local": fixture("local", count=22),
        "gmail": fixture("gmail", "gmail", count=4),
        "restored": fixture("restored"),
        "expired": fixture("expired"),
        "running": fixture("running"),
        "lost": fixture("lost"),
    }
    plans["gmail"].update(total_matched=27, remaining_count=23, missing_date_count=2)
    for terminal in ("done", "partial", "failed"):
        receipt = fixture(f"history-{terminal}")
        complete(receipt)
        receipt["status"] = terminal
        if terminal != "done":
            receipt["result"].update(changed=0, failed=1)
        plans[f"history-{terminal}"] = receipt
    conversations = [
        {
            "id": key,
            "title": key.title(),
            "course_id": None,
            "messages": [
                {
                    "id": f"answer-{key}",
                    "role": "assistant",
                    "content": "Review this deletion plan.",
                    "activity": [
                        {
                            "id": f"activity-{key}",
                            "label": "Prepared deletion plan",
                            "status": "done",
                            "mail_deletion_plan": copy.deepcopy(value) | {"version": "old-version"},
                        }
                    ],
                }
            ],
        }
        for key, value in plans.items()
    ]
    complete(plans["restored"])
    plans["running"]["status"] = "running"
    applies = []
    errors = []

    async def route_api(route):
        request = route.request
        path = unquote(urlsplit(request.url).path)
        result = {}
        status = 200
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
            result = {"model": "", "base_url": "", "has_api_key": False}
        elif path == "/api/chat/conversations":
            result = {"conversations": conversations}
        elif path.startswith("/api/chat/conversations/"):
            result = next(item for item in conversations if item["id"] == path.rsplit("/", 1)[1])
        elif path.startswith("/api/chat/inbox-plans/"):
            plan_id = path.split("/")[4]
            if plan_id == "expired" or plan_id.startswith("history-"):
                status = 404
                result = {"detail": "Plan not found"}
            elif request.method == "POST":
                body = request.post_data_json
                assert body == {"expected_version": "latest-version"}, body
                applies.append((plan_id, body))
                await asyncio.sleep(0.15)
                plan = plans[plan_id]
                if plan_id == "gmail":
                    plan.update(
                        status="partial",
                        result={
                            "changed": 1,
                            "failed": 1,
                            "unverified": 1,
                            "skipped": 1,
                            "results": [
                                {"id": "gmail-0", "status": "trashed"},
                                {
                                    "id": "gmail-1",
                                    "status": "failed",
                                    "error": "Button unavailable",
                                },
                                {
                                    "id": "gmail-2",
                                    "status": "unverified",
                                    "error": "Trash state unavailable",
                                },
                                {"id": "gmail-3", "status": "skipped"},
                            ],
                        },
                    )
                else:
                    complete(plan)
                result = plan
                if plan_id == "lost":
                    status = 503
                    result = {"detail": "Response interrupted"}
            else:
                result = plans[plan_id]
        elif path.startswith("/api/mail"):
            result = {
                "messages": [],
                "total": 0,
                "has_more": False,
                "connection": {"status": "disconnected"},
            }
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

        async def select(title, destination="Local Inbox"):
            await page.locator(".chat-history-select").filter(has_text=title).click()
            panel = page.get_by_role("region", name=f"{destination} deletion plan", exact=True)
            await expect(panel).to_be_visible()
            return panel

        panel = await select("Local")
        button = panel.get_by_role("button", name="Delete locally", exact=True)
        await expect(button).to_be_enabled()
        assert not applies, "Preparing and viewing plans must never apply them"
        await expect(page.locator(".chat-activity")).not_to_have_attribute("open", "")
        await panel.locator("summary").click()
        await expect(panel.locator("li")).to_have_count(20)
        await panel.get_by_role("button", name="Show more", exact=True).click()
        await expect(panel.locator("li")).to_have_count(22)
        await button.evaluate("element => { element.click(); element.click(); }")
        await expect(panel).to_contain_text("22 deleted locally")
        assert [item[0] for item in applies] == ["local"]
        panel = await select("Gmail", "Gmail")
        await expect(panel).to_contain_text("entire Gmail conversation to Trash")
        await expect(panel).to_contain_text("23 are outside this plan")
        await expect(panel).to_contain_text("unknown dates were excluded")
        await panel.get_by_role("button", name="Move to Gmail Trash", exact=True).click()
        await expect(panel).to_contain_text("Partially completed")
        await expect(panel).to_contain_text("1 failed")
        await expect(panel).to_contain_text("1 unverified")
        await expect(panel).to_contain_text("1 skipped")
        await expect(
            panel.get_by_role("button", name="Move to Gmail Trash", exact=True)
        ).to_have_count(0)
        await panel.locator("summary").click()
        await expect(panel).to_contain_text("Trash state unavailable")
        Path("data/debug").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path="data/debug/chat-mail-plan-desktop.png", full_page=True)
        panel = await select("Restored")
        await expect(panel).to_contain_text("1 deleted locally")
        await expect(panel.get_by_role("button", name="Delete locally", exact=True)).to_have_count(
            0
        )
        for terminal in ("done", "partial", "failed"):
            panel = await select(f"History-{terminal.title()}")
            await expect(panel.get_by_text("Checking status…", exact=True)).to_have_count(0)
            await expect(panel).not_to_contain_text("expired")
            await expect(panel).to_contain_text(
                "1 deleted locally" if terminal == "done" else "1 failed"
            )
            await expect(
                panel.get_by_role("button", name="Delete locally", exact=True)
            ).to_have_count(0)
        panel = await select("Expired")
        await expect(panel).to_contain_text("expired. Search again")
        await expect(panel.get_by_role("button", name="Delete locally", exact=True)).to_have_count(
            0
        )
        panel = await select("Running")
        await expect(panel).to_contain_text("Deleting…")
        await expect(panel.get_by_role("button", name="Delete locally", exact=True)).to_have_count(
            0
        )
        complete(plans["running"])
        await panel.get_by_role("button", name="Refresh status", exact=True).click()
        await expect(panel).to_contain_text("1 deleted locally")
        panel = await select("Lost")
        await panel.get_by_role("button", name="Delete locally", exact=True).click()
        await expect(panel).to_contain_text("1 deleted locally")
        await expect(panel.get_by_role("button", name="Delete locally", exact=True)).to_have_count(
            0
        )
        assert [item[0] for item in applies] == ["local", "gmail", "lost"]
        panel = await select("Gmail", "Gmail")
        await expect(panel).to_contain_text("Partially completed")
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=f"data/debug/chat-mail-plan-{width}.png", full_page=True)
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
        "Mail plan UI smoke passed: local/Gmail confirmation, pagination, fresh versions, "
        "partial results, expired/running plans, lost response recovery and mobile."
    )


if __name__ == "__main__":
    main()
