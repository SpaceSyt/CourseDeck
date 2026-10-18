"""Deterministic Inbox races with synthetic responses, no backend or saved data."""

import asyncio
import copy
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
import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Inbox} from './src/Inbox';
function Harness() {
  const [openId, setOpenId] = useState(null);
  const [mounted, setMounted] = useState(true);
  return <><button onClick={() => setOpenId('a')}>Open A</button>
    <button onClick={() => setOpenId('b')}>Open B</button>
    <button onClick={() => setMounted(!mounted)}>Toggle inbox</button>
    {mounted && <Inbox courses={[]} addTask={() => {}} openId={openId}
      opened={() => setOpenId(null)}/>}</>;
}
createRoot(document.getElementById('root')).render(<Harness/>);
"""
HTML = """<!doctype html><meta charset="utf-8"><title>Inbox race fixture</title>
<div id="root"></div><script src="mail-ui.js"></script>"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Gate:
    next_id = 0

    def __init__(self, method, key, status=200):
        Gate.next_id += 1
        self.id = str(Gate.next_id)
        self.method, self.key, self.status = method, key, status
        self.started, self.release, self.done = (asyncio.Event() for _ in range(3))

    async def wait(self):
        await asyncio.wait_for(self.started.wait(), timeout=5)

    async def finish(self, page):
        self.release.set()
        await asyncio.wait_for(self.done.wait(), timeout=5)
        # Synchronize with response parsing and React commits, never a timed sleep.
        await page.wait_for_function("id => window.fixtureCompleted.includes(id)", arg=self.id)
        await page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
        )


async def check(base_url):
    messages = {
        key: {
            "id": key,
            "subject": f"Message {key.upper()}",
            "sender": "Fixture",
            "sender_email": "fixture@example.test",
            "classification": "none",
            "course_override": "auto",
            "categories": [],
            "body_complete": True,
            "body": "Synthetic message",
            "url": "",
            "deleted": False,
            "starred": False,
            "unread": True,
        }
        for key in ("a", "b")
    }
    gates, writes, errors = [], [], []

    def hold(method, key, status=200):
        gate = Gate(method, key, status)
        gates.append(gate)
        return gate

    async def route_api(route):
        request = route.request
        path = urlsplit(request.url).path
        if not request.url.startswith(base_url + "/"):
            errors.append("Unexpected external request")
            await route.abort()
            return
        if not path.startswith("/api/"):
            await route.continue_()
            return
        if path == "/api/mail":
            await route.fulfill(
                json={"messages": list(messages.values()), "total": 2, "has_more": False}
            )
            return
        assert path.startswith("/api/mail/messages/"), path
        key = path.rsplit("/", 1)[-1]
        if request.method == "PATCH":
            values = request.post_data_json
            writes.append((key, values))
            messages[key].update({k: v for k, v in values.items() if k != "read"})
            if "read" in values:
                messages[key]["unread"] = not values["read"]
        response = copy.deepcopy(messages[key])
        gate = next((g for g in gates if (g.method, g.key) == (request.method, key)), None)
        if gate:
            gates.remove(gate)
            gate.started.set()
            await gate.release.wait()
        await route.fulfill(
            status=gate.status if gate else 200,
            headers={"X-Fixture-Gate": gate.id} if gate else {},
            json=response if not gate or gate.status == 200 else {"detail": "Stale failure"},
        )
        if gate:
            gate.done.set()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script("""
              window.fixtureCompleted = [];
              const fetch = window.fetch;
              window.fetch = async (...args) => {
                const response = await fetch(...args);
                const json = response.json.bind(response);
                response.json = async () => {
                  try { return await json(); }
                  finally { window.fixtureCompleted.push(response.headers.get('X-Fixture-Gate')); }
                };
                return response;
              };
            """)
            await page.route("**/*", route_api)
            await page.goto(base_url + "/mail-ui.html")
            inbox = page.get_by_role("complementary", name="Inbox", exact=True)
            title = inbox.locator("h3")

            async def changed():
                await page.evaluate("window.dispatchEvent(new Event('coursedeck:inbox-changed'))")

            async def open_mail(key):
                await page.get_by_role("button", name=f"Open {key.upper()}", exact=True).click()

            # The event arrives after PATCH applied, before its obsolete response is delivered.
            opening = hold("PATCH", "a")
            await open_mail("a")
            await opening.wait()
            messages["a"].update(deleted=True, unread=True)
            await changed()
            await expect(inbox.get_by_label("Restore email", exact=True)).to_be_visible()
            await opening.finish(page)
            await expect(inbox.get_by_label("Restore email", exact=True)).to_be_visible()
            assert messages["a"]["unread"]

            # Newest GET wins, and old failed GET cannot leave a misleading error.
            for status in (200, 503):
                stale = hold("GET", "a", status)
                await changed()
                await stale.wait()
                messages["a"]["deleted"] = False
                await changed()
                await expect(inbox.get_by_label("Delete email locally", exact=True)).to_be_visible()
                await stale.finish(page)
                await expect(inbox.get_by_label("Delete email locally", exact=True)).to_be_visible()
                await expect(inbox.get_by_role("alert")).to_have_count(0)

            # Repeated actions in one browser turn send one write; stale PATCH body is ignored.
            mutation = hold("PATCH", "a")
            before = len(writes)
            await inbox.get_by_label("Star email", exact=True).evaluate(
                "e => {e.click();e.click()}"
            )
            await mutation.wait()
            messages["a"]["deleted"] = True
            await changed()
            await expect(inbox.get_by_label("Restore email", exact=True)).to_be_visible()
            await mutation.finish(page)
            assert len(writes) == before + 1
            await expect(inbox.get_by_label("Restore email", exact=True)).to_be_visible()

            # A -> B -> A keeps the latest opening, even with both older responses delayed.
            await inbox.get_by_label("Back to inbox", exact=True).click()
            first_a, first_b = hold("PATCH", "a"), hold("PATCH", "b")
            await open_mail("a")
            await first_a.wait()
            await open_mail("a")  # duplicate open while pending
            await open_mail("b")
            await first_b.wait()
            await open_mail("a")
            await expect(title).to_have_text("Message A")
            await first_b.finish(page)
            await first_a.finish(page)
            await expect(title).to_have_text("Message A")
            assert [key for key, _ in writes[-3:]] == ["a", "b", "a"]

            # Back while opening and unmount both invalidate pending responses/errors.
            await inbox.get_by_label("Back to inbox", exact=True).click()
            cancelled = hold("PATCH", "b")
            await open_mail("b")
            await cancelled.wait()
            await inbox.get_by_label("Back to inbox", exact=True).click()
            await cancelled.finish(page)
            await expect(inbox.locator(".mail-row")).to_have_count(2)
            await expect(title).to_have_count(0)
            await open_mail("a")
            await expect(title).to_have_text("Message A")
            failed = hold("GET", "a", 503)
            await changed()
            await failed.wait()
            await page.get_by_role("button", name="Toggle inbox", exact=True).click()
            await failed.finish(page)
            await page.get_by_role("button", name="Toggle inbox", exact=True).click()
            await expect(inbox.locator(".mail-row")).to_have_count(2)
            await expect(inbox.get_by_role("alert")).to_have_count(0)

            # A mutation failure after switching cannot leak an error into the new email.
            await open_mail("a")
            await expect(title).to_have_text("Message A")
            failed_write = hold("PATCH", "a", 503)
            await inbox.get_by_label("Unstar email", exact=True).click()
            await failed_write.wait()
            await open_mail("b")
            await expect(title).to_have_text("Message B")
            await failed_write.finish(page)
            await expect(inbox.get_by_role("alert")).to_have_count(0)

            # An initial GET failure ends loading and permits retrying the same email.
            await inbox.get_by_label("Back to inbox", exact=True).click()
            failed_open = hold("GET", "a", 503)
            await open_mail("a")
            await failed_open.wait()
            await failed_open.finish(page)
            await expect(inbox.locator(".mail-row")).to_have_count(2)
            await expect(inbox.get_by_role("alert")).to_contain_text("Could not refresh")
            await open_mail("a")
            await expect(title).to_have_text("Message A")
            await expect(inbox.get_by_role("alert")).to_have_count(0)
            assert not errors, errors
        finally:
            await browser.close()


def main():
    frontend = ROOT / "frontend"
    esbuild = frontend / "node_modules/.bin" / ("esbuild.cmd" if os.name == "nt" else "esbuild")
    with tempfile.TemporaryDirectory(prefix="mail-detail-smoke-") as temporary:
        directory = Path(temporary)
        subprocess.run(
            [
                str(esbuild),
                "--bundle",
                "--jsx=automatic",
                "--loader=tsx",
                "--sourcefile=mail-smoke.tsx",
                "--outfile=" + str(directory / "mail-ui.js"),
            ],
            input=HARNESS,
            text=True,
            cwd=frontend,
            check=True,
            timeout=60,
        )
        (directory / "mail-ui.html").write_text(HTML, encoding="utf-8")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(directory))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(
        "Inbox race smoke passed: delayed open/mutation/GET, unread, repeat, switch, back, unmount."
    )


if __name__ == "__main__":
    main()
