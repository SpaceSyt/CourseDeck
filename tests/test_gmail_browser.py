from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from coursedeck.db import Database
from coursedeck.gmail_browser import GmailBrowser


async def test_browser_sync_caches_threads_and_keeps_local_deletion(tmp_path):
    db = Database(tmp_path / "db")
    db.update_state("gmail", account="student@example.test", authorized=True)
    adapter = GmailBrowser(db, tmp_path)
    reads = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        await context.expose_function("recordRead", lambda: reads.append(True))
        html = """<header><a role="button"
          aria-label="Google Account: Student (student@example.test)">Account</a></header>
        <div class="a3s" style="display:none">Previously viewed email</div>
        <main role="main"><table><tr class="zA zE"><td class="yW">
        <span email="teacher@example.test" name="Instructor">Instructor</span></td>
        <td><span class="bog" data-legacy-thread-id="123" data-legacy-last-message-id="m1"
        onclick="readThread()">Survey</span>
        <span class="y2"> - Please complete the survey.</span></td><td class="xW">
        <span title="Sep 8, 2026, 13:21">13:21</span></td></tr></table></main>"""
        html += """<script>
        const original = document.querySelector('main').innerHTML;
        function readThread() {
          recordRead();
          document.querySelector('main').innerHTML =
            '<div class=kv>Collapsed previous message</div>'
            + '<button onclick="expandThread()">Expand all</button>'
            + '<div class=a3s style="display:none">Please complete the survey. '
            + '<a href="https://example.test/survey">Open survey</a></div>';
          location.hash = 'inbox/thread123';
        }
        function expandThread() {
          document.querySelector('main .kv').remove();
          document.querySelector('main .a3s').style.display = 'block';
        }
        addEventListener('hashchange', () => {
          if (location.hash === '#inbox') document.querySelector('main').innerHTML = original;
        });
        </script>"""
        await context.route(
            "https://mail.google.com/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )

        class Session:
            @asynccontextmanager
            async def session(self, timezone=None):
                yield context

        adapter.browser = Session()
        await adapter._sync()
        mail = adapter.store.get("gmail:123")
        assert mail["body"].startswith("Please complete the survey.")
        assert "Open survey: https://example.test/survey" in mail["body"]
        assert mail["body_complete"] and mail["classification"] == "none"
        assert mail["date_text"] == "13:21" and mail["sender_email"] == "teacher@example.test"
        assert len(reads) == 1
        adapter.store.patch("gmail:123", {"deleted": True, "starred": True})
        await adapter._sync()
        assert len(reads) == 1  # Unchanged threads are not opened again.
        assert adapter.store.list()["total"] == 0
        assert adapter.store.get("gmail:123")["starred"]
        assert db.state("gmail")["last_sync"]
        await browser.close()
