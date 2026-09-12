"""Single Gmail mailbox using a dedicated user-authorized browser profile.

Opening a thread may mark it read in Gmail. No send, delete, archive or label
controls are used. Cached mail and all other state changes remain local.
"""

import asyncio
import re
from collections import Counter
from datetime import timedelta

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from .browser import BrowserManager, installed_chrome_channel
from .domain import now
from .gmail_sync import advance_history, history_page, parse_received_at, select_body_ids
from .mail import MailMessage, MailStore

INBOX = "https://mail.google.com/mail/u/0/#inbox"
BODY_READS_PER_PAGE = 8
VISIBLE_ROWS = "tr.zA:visible"


class GmailLoginRequired(ValueError):
    pass


# Gmail's visible inbox rows. Never replay private RPCs or copy session tokens.
ROWS = r"""rows => rows.map(row => {
  const subject = row.querySelector('.bog');
  const identity = row.querySelector('[data-legacy-thread-id], [data-thread-id]');
  const sender = row.querySelector('.yW [email]') || row.querySelector('.yW [name]') ||
                 row.querySelector('.yW');
  const date = row.querySelector('.xW [title]') || row.querySelector('.xW');
  return {
    id: identity?.getAttribute('data-legacy-thread-id') ||
        identity?.getAttribute('data-thread-id') || '',
    sender: sender?.getAttribute('name') || sender?.textContent?.trim() || '',
    sender_email: sender?.getAttribute('email') || '',
    subject: subject?.textContent?.trim() || '',
    snippet: row.querySelector('.y2')?.textContent?.replace(/^\s*[-–]\s*/, '').trim() || '',
    date_label: date?.getAttribute('title') || date?.textContent?.trim() || '',
    date_text: date?.textContent?.trim() || '',
    content_key: 'text-links-v1:' + (
                 identity?.getAttribute('data-legacy-last-non-draft-message-id') ||
                 identity?.getAttribute('data-legacy-last-message-id') || ''),
    unread: row.classList.contains('zE'),
  };
})"""

BODY = r"""() => {
  const bodies = [...document.querySelectorAll('.a3s')];
  const visible = bodies.filter(e => e.getClientRects().length);
  const text = visible.map(e => {
    const body = e.innerText;
    const links = [...e.querySelectorAll('a[href]')].filter(a =>
      /^https?:\/\//i.test(a.href) && !body.includes(a.href)
    ).map(a => `${a.innerText.trim() || 'Link'}: ${a.href}`);
    return [body, ...new Set(links)].join('\n');
  }).join('\n\n────────\n\n');
  return {
    body: text,
    body_complete: visible.length > 0 &&
      ![...document.querySelectorAll('.kv')].some(e => e.getClientRects().length),
  };
}"""


class GmailBrowser:
    def __init__(self, db, data_dir):
        self.db = db
        self.store = MailStore(db)
        self.browser = BrowserManager(data_dir, "gmail", installed_chrome_channel())
        self.lock = asyncio.Lock()
        self.job = None

    def status(self):
        state = self.db.state("gmail")
        return {
            "status": "login_pending"
            if self.browser.interactive
            else "connected"
            if state.get("authorized") and self.browser.exists()
            else "not_connected",
            "syncing": bool(self.job and not self.job.done()),
            "account": state.get("account"),
            "last_sync": state.get("last_sync"),
            "error": state.get("error"),
            "error_code": state.get("error_code"),
            "warning": state.get("warning"),
            "coverage": state.get("coverage"),
        }

    async def account(self, page):
        labels = await page.locator(
            'header a[aria-label*="@"], [role="banner"] a[aria-label*="@"], '
            'a[href*="accounts.google.com"][aria-label]'
        ).evaluate_all("els => els.map(e => e.getAttribute('aria-label'))")
        for label in labels:
            if label and "@" in label:
                found = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", label)
                if found:
                    return found[0].casefold()
        # Gmail's inbox title includes the signed-in email, including Workspace branding.
        title_accounts = set(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", await page.title()))
        if len(title_accounts) == 1:
            return title_accounts.pop().casefold()
        return None

    async def connect(self):
        if self.lock.locked():
            raise ValueError("Wait for mail sync to finish")
        async with self.lock:
            await self.browser.login(INBOX, self.db.settings().timezone)
        return {"message": "Sign in to Gmail, then select Finish login."}

    async def finish_login(self):
        if self.lock.locked():
            raise ValueError("Wait for mail sync to finish")
        async with self.lock:
            context = self.browser.interactive
            if not context:
                raise ValueError("Open Gmail login first")
            page = next(
                (p for p in context.pages if p.url.startswith("https://mail.google.com/")), None
            )
            if not page:
                raise ValueError("Finish signing in to Gmail first")
            await page.goto(INBOX, wait_until="domcontentloaded")
            try:
                await page.locator('[role="main"]').first.wait_for(timeout=30000)
            except BrowserTimeout as exc:
                raise ValueError("Gmail inbox is not ready. Complete login and retry.") from exc
            account = await self.account(page)
            if not account:
                raise ValueError(
                    "Could not identify the Gmail account. Keep the inbox open and retry."
                )
            old = self.db.state("gmail").get("account")
            if old and old != account:
                raise ValueError(
                    "This inbox is linked to another account. Sign in with the original account."
                )
            self.db.update_state(
                "gmail", account=account, authorized=True, error=None, error_code=None
            )
            await self.browser.close_login()
        self.spawn()
        return {"message": ""}

    def spawn(self):
        if self.status()["status"] != "connected" or self.lock.locked():
            return
        if not self.job or self.job.done():
            self.job = asyncio.create_task(self.sync())

    async def poll(self):
        while True:
            await asyncio.sleep(300)
            self.spawn()

    async def sync(self):
        async with self.lock:
            self.db.update_state("gmail", error=None, error_code=None)
            try:
                async with asyncio.timeout(240):
                    await self._sync()
            except asyncio.CancelledError:
                raise
            except GmailLoginRequired:
                self.db.update_state(
                    "gmail",
                    authorized=False,
                    error_code="auth_required",
                    error="Gmail sign-in expired. Reconnect to continue syncing.",
                )
            except (TimeoutError, BrowserError):
                self.db.update_state(
                    "gmail",
                    error_code="network_error",
                    error="Gmail did not finish loading. Retry sync.",
                )
            except Exception:
                self.db.update_state(
                    "gmail",
                    error_code="read_error",
                    error="Could not read the Gmail inbox. Retry sync.",
                )

    async def _inbox_page(self, page, number=1, page_size=None):
        url = INBOX if number == 1 else f"{INBOX}/p{number}"
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if "accounts.google.com" in page.url:
            raise GmailLoginRequired()
        await page.locator('[role="main"]').first.wait_for(timeout=30000)
        account = await self.account(page)
        if not account or account != self.db.state("gmail").get("account"):
            if account:
                raise GmailLoginRequired("Mailbox changed")
            raise ValueError("Mailbox identity not ready")
        # A loading or unfamiliar empty layout is never a successful empty inbox.
        await page.wait_for_function(
            """() =>
            [...document.querySelectorAll('tr.zA')].some(e=>e.getClientRects().length) ||
            [...document.querySelectorAll('[role="main"]')].some(e=>
                e.getClientRects().length &&
                /^(No conversations in Inbox[.]|Your inbox is empty[.])$/.test(e.innerText.trim()))
        """,
            timeout=15000,
        )
        if number == 1 or page_size:
            expected = 1 if number == 1 else (number - 1) * page_size + 1
            await page.wait_for_function(
                r"""start => {
                const ranges = [...document.querySelectorAll('.Dj')]
                  .filter(e=>e.getClientRects().length)
                  .map(e=>e.innerText.replaceAll(',', '').match(/^\s*(\d+)[–-]\d+ of \d+/))
                  .filter(Boolean);
                return !ranges.length || (ranges.length === 1 && Number(ranges[0][1]) === start);
                }
            """,
                arg=expected,
                timeout=15000,
            )
        rows = await page.locator(VISIBLE_ROWS).evaluate_all(ROWS)
        labels = await page.locator(".Dj:visible").all_text_contents()
        ranges = []
        for label in labels:
            match = re.fullmatch(r"\s*([\d,]+)[–-]([\d,]+) of ([\d,]+)\s*", label)
            if match:
                ranges.append(tuple(int(value.replace(",", "")) for value in match.groups()))
        position = ranges[0] if len(ranges) == 1 else None
        if position and (position[1] - position[0] + 1 != len(rows) or position[1] > position[2]):
            raise ValueError("Gmail page range did not match visible rows")
        older = page.get_by_role("button", name="Older", exact=True)
        has_older = None
        if position and await older.count() == 1:
            has_older = await older.is_enabled() and (
                await older.get_attribute("aria-disabled") != "true"
            )
            if position and has_older != (position[1] < position[2]):
                raise ValueError("Gmail pagination changed while reading")
        if not rows:
            if number != 1 or has_older is True:
                raise ValueError("Gmail page is empty without confirmed inbox coverage")
            has_older = False
        return rows, has_older, position

    def _headers(self, rows):
        cached, invalid, undated = {}, 0, 0
        with self.db.connection() as conn:
            oldest = conn.execute("SELECT MIN(received_at) FROM mail_messages").fetchone()[0]
        from datetime import datetime

        fallback = datetime.fromisoformat(oldest) if oldest else now()
        seen = set()
        for rank, row in enumerate(rows):
            if not row["id"] or row["id"] in seen:
                invalid += 1
                continue
            seen.add(row["id"])
            key = "gmail:" + row["id"]
            try:
                cached[row["id"]] = self.store.get(key)
            except KeyError:
                pass
            value = MailMessage(**(row | {"id": key}))
            value.received_at = parse_received_at(value.date_label, self.db.settings().timezone)
            if value.received_at is None:
                undated += 1
            sort_at = None
            if value.received_at is None and row["id"] not in cached:
                sort_at = (fallback - timedelta(microseconds=rank + 1)).isoformat()
            self.store.upsert(value, sort_at)
        return cached, invalid, undated

    async def _bodies(self, page, rows, cached, number, page_size):
        counts = Counter(row["id"] for row in rows)
        ambiguous = {key for key, count in counts.items() if count > 1}
        rows = [row for row in rows if row["id"] not in ambiguous]
        attempts = {key: value.get("body_checked_at") for key, value in cached.items()}
        selected = select_body_ids(rows, cached, attempts, BODY_READS_PER_PAGE)
        by_id = {row["id"]: row for row in rows if row["id"]}
        failures = len(ambiguous)
        for remote_id in selected:
            value = MailMessage(**(by_id[remote_id] | {"id": "gmail:" + remote_id}))
            value.received_at = parse_received_at(value.date_label, self.db.settings().timezone)
            value.body_checked_at = now()
            self.store.upsert(value)
            current_rows = page.locator(VISIBLE_ROWS)
            current = await current_rows.evaluate_all(ROWS)
            index = next((i for i, item in enumerate(current) if item["id"] == remote_id), None)
            if index is None:
                failures += 1
                continue
            if any(
                current[index].get(field) != by_id[remote_id].get(field)
                for field in ("content_key", "subject", "snippet", "date_label")
            ):
                # A live thread changed after its headers were captured. Retry next run.
                failures += 1
                continue
            try:
                await current_rows.nth(index).locator(".bog").first.click()
                await page.locator(".a3s, .kv").first.wait_for(state="attached", timeout=15000)
                expand = page.get_by_role("button", name="Expand all", exact=True)
                if await page.locator(".kv:visible").count() and await expand.count():
                    await expand.click()
                    try:
                        await page.locator(".kv:visible").first.wait_for(
                            state="hidden", timeout=10000
                        )
                    except BrowserTimeout:
                        pass
                await page.locator(".a3s:visible").first.wait_for(timeout=15000)
                content = await page.evaluate(BODY)
                value.body = content["body"][:500000]
                value.body_complete = content["body_complete"] and len(content["body"]) <= 500000
                value.url = page.url
                self.store.upsert(value)
                if not value.body_complete:
                    failures += 1
            except BrowserTimeout:
                if "accounts.google.com" in page.url:
                    raise GmailLoginRequired() from None
                failures += 1
            await self._inbox_page(page, number, page_size)
        return failures

    async def _sync(self):
        async with self.browser.session(self.db.settings().timezone) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            progress = self.db.state("gmail").get("inbox_progress", {})
            rows, older, position = await self._inbox_page(page)
            page_size = len(rows)
            cached, skipped, undated = self._headers(rows)
            # Commit headers and progress before any slow, cancellable body reads.
            coverage = {
                "scope": "inbox",
                "listed_this_run": len(rows) - skipped,
                "source_total": position[2] if position else None,
                "history_next_page": history_page(progress),
                "pagination_known": older is not None,
            }
            self.db.update_state(
                "gmail", coverage=coverage, warning="Inbox headers cached; reading continues."
            )
            body_pages = [(1, rows, cached)]
            if older is True:
                target = history_page(progress)
                # A shrinking mailbox may remove the stored page position.
                if position and (target - 1) * page_size >= position[2]:
                    target = 2
                    progress = dict(progress, next_page=2)
                history, has_older, page_range = await self._inbox_page(page, target, page_size)
                previous, missing, dates = self._headers(history)
                skipped += missing
                undated += dates
                coverage["listed_this_run"] += len(history) - missing
                progress = advance_history(
                    progress, target, headers_complete=not missing, has_older=has_older
                )
                coverage["history_next_page"] = history_page(progress)
                coverage["pagination_known"] &= has_older is not None
                self.db.update_state("gmail", inbox_progress=progress, coverage=coverage)
                body_pages.append((target, history, previous))
            elif older is False:
                self.db.update_state("gmail", inbox_progress={"next_page": 2})
            failures = 0
            for number, headers, previous in body_pages:
                try:
                    # One slow page cannot consume the other page's entire body budget.
                    async with asyncio.timeout(80):
                        await self._inbox_page(page, number, page_size)
                        failures += await self._bodies(page, headers, previous, number, page_size)
                except (TimeoutError, BrowserTimeout):
                    failures += 1
            with self.db.connection() as conn:
                incomplete = conn.execute(
                    "SELECT COUNT(*) FROM mail_messages WHERE "
                    "COALESCE(json_extract(payload, '$.body_complete'),0)=0"
                ).fetchone()[0]
            coverage["cached_bodies_pending"] = incomplete
            warnings = []
            if older is True:
                warnings.append(
                    f"Older inbox pages continue automatically (next: {history_page(progress)})."
                )
            if older is None or not coverage["pagination_known"]:
                warnings.append("Inbox pagination could not be verified.")
            if incomplete:
                warnings.append(f"{incomplete} cached conversation bodies await reading.")
            if skipped or failures:
                warnings.append(
                    "Some conversations could not be fully read; cached content retained."
                )
            if undated:
                warnings.append("Some dates are unrecognized; their order is uncertain.")
            self.db.update_state(
                "gmail",
                last_sync=now().isoformat(),
                error=None,
                error_code=None,
                warning=" ".join(warnings) or None,
                coverage=coverage,
            )

    async def disconnect(self):
        if self.lock.locked():
            raise ValueError("Wait for mail sync to finish")
        async with self.lock:
            await self.browser.reset()
            self.db.update_state("gmail", authorized=False, error=None, error_code=None)
        return {"message": ""}

    async def close(self):
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.browser.close()
