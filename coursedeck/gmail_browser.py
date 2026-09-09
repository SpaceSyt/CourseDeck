"""Single Gmail mailbox using a dedicated user-authorized browser profile.

Opening a thread may mark it read in Gmail. No send, delete, archive or label
controls are used. Cached mail and all other state changes remain local.
"""

import asyncio
import re
from datetime import timedelta

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from .browser import BrowserManager, installed_chrome_channel
from .domain import now
from .mail import MailMessage, MailStore

INBOX = "https://mail.google.com/mail/u/0/#inbox"


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

    async def _sync(self):
        async with self.browser.session(self.db.settings().timezone) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(INBOX, wait_until="domcontentloaded", timeout=45000)
            if "accounts.google.com" in page.url:
                raise GmailLoginRequired()
            await page.locator('[role="main"]').first.wait_for(timeout=30000)
            account = await self.account(page)
            if account != self.db.state("gmail").get("account") or not account:
                if account:
                    raise GmailLoginRequired("Mailbox changed")
                raise ValueError("Mailbox identity not ready")
            try:
                await page.locator("tr.zA").first.wait_for(timeout=15000)
            except BrowserTimeout:
                # Unknown layouts must not silently look like a successful empty sync.
                raise ValueError("No recognizable Gmail rows") from None
            rows = await page.locator("tr.zA").evaluate_all(ROWS)
            skipped = sum(not row["id"] for row in rows[:30])
            if skipped == len(rows[:30]):
                raise ValueError("No stable mail identifiers")
            snapshot_time = now()
            for rank, row in enumerate(rows[:30]):
                if not row["id"]:
                    continue
                remote_id = row["id"]
                key = "gmail:" + remote_id
                old = None
                try:
                    old = self.store.get(key)
                except KeyError:
                    pass
                value = MailMessage(**(row | {"id": key}))
                # New content in an existing thread must be read again.
                if (
                    old
                    and old["snippet"] == value.snippet
                    and old["date_label"] == value.date_label
                    and old["body_complete"]
                    and old.get("content_key", "") == value.content_key
                ):
                    value.url = old["url"]
                    self.store.upsert(value, (snapshot_time - timedelta(seconds=rank)).isoformat())
                    continue
                self.store.upsert(value, (snapshot_time - timedelta(seconds=rank)).isoformat())
                current_rows = page.locator("tr.zA")
                current = await current_rows.evaluate_all(ROWS)
                index = next((i for i, item in enumerate(current) if item["id"] == remote_id), None)
                if index is None:
                    skipped += 1
                    continue
                try:
                    await current_rows.nth(index).locator(".bog").first.click()
                    # The newest message can consist entirely of trimmed/duplicate content.
                    # Expand the conversation before waiting for a visible body.
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
                    value.body_complete = (
                        content["body_complete"] and len(content["body"]) <= 500000
                    )
                    value.url = page.url
                    self.store.upsert(value)
                except BrowserTimeout:
                    if "accounts.google.com" in page.url:
                        raise GmailLoginRequired() from None
                    skipped += 1
                await page.goto(INBOX, wait_until="domcontentloaded")
                await page.locator("tr.zA").first.wait_for(timeout=15000)
            self.db.update_state(
                "gmail",
                last_sync=now().isoformat(),
                error=None,
                error_code=None,
                warning="Latest 30 inbox threads only."
                + (" Some messages could not be read." if skipped else ""),
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
