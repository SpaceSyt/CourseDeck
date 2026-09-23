from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from coursedeck import gmail_browser
from coursedeck.db import Database
from coursedeck.gmail_browser import INBOX, GmailBrowser, gmail_url
from coursedeck.mail import MailMessage

ACCOUNT = "https://accounts.google.com/synthetic-session"
MAILBOX = """<header><a aria-label="Google Account: Student (student@example.test)">
Account</a></header><main role="main">Your inbox is empty.</main>"""


@pytest.fixture
async def mailbox(tmp_path, monkeypatch):
    db = Database(tmp_path / "mail.db")
    db.update_state("gmail", account="student@example.test", authorized=True, last_sync="old")
    adapter = GmailBrowser(db, tmp_path)
    adapter.store.upsert(
        MailMessage(id="gmail:cached", sender="Instructor", subject="Keep this mail")
    )
    adapter.store.patch("gmail:cached", {"starred": True, "deleted": True})
    resume = gmail_browser.resume_session

    async def resume_quickly(page, is_ready, **options):
        return await resume(page, is_ready, **options, timeout_ms=1200)

    monkeypatch.setattr(gmail_browser, "resume_session", resume_quickly)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await context.route(
            "https://mail.google.com/**",
            lambda route: route.fulfill(body=MAILBOX, content_type="text/html"),
        )

        class Session:
            interactive = None
            close_login = AsyncMock()

            @asynccontextmanager
            async def session(self, timezone=None):
                yield context

        adapter.browser = Session()
        try:
            yield adapter, context, page
        finally:
            await browser.close()


def redirect_navigation(page, monkeypatch, target):
    goto = page.goto

    async def redirected_goto(url, **options):
        # Redirect hops bypass Playwright routing; use the intercepted entry page.
        return await goto(target if gmail_url(url) else url, **options)

    monkeypatch.setattr(page, "goto", redirected_goto)


async def test_passive_google_redirect_syncs_without_reconnect(mailbox, monkeypatch):
    adapter, context, page = mailbox
    redirect_navigation(page, monkeypatch, ACCOUNT)
    await context.route(
        ACCOUNT,
        lambda route: route.fulfill(
            body=f'<script>setTimeout(() => location.href = "{INBOX}", 100)</script>',
            content_type="text/html",
        ),
    )
    await adapter.sync()
    state = adapter.db.state("gmail")
    assert state["authorized"] and state["error_code"] is None
    assert state["last_sync"] != "old"
    cached = adapter.store.get("gmail:cached")
    assert cached["starred"] and cached["deleted"]


async def test_account_chooser_resumes_only_the_linked_mailbox(mailbox, monkeypatch):
    adapter, context, page = mailbox
    chooser = "https://accounts.google.com/v3/signin/accountchooser"
    redirect_navigation(page, monkeypatch, chooser)
    selected = []
    await context.expose_function("selectedAccount", lambda value: selected.append(value))
    await context.route(
        chooser,
        lambda route: route.fulfill(
            body=f"""<button data-identifier="other@example.test"
            onclick="selectedAccount('other')">Other</button>
            <button data-identifier="student@example.test"
            onclick="selectedAccount('linked'); location.href='{INBOX}'">Student</button>""",
            content_type="text/html",
        ),
    )
    await adapter.sync()
    assert adapter.db.state("gmail")["error_code"] is None
    assert selected and set(selected) == {"linked"}


@pytest.mark.parametrize(
    "target,body,error_code,authorized",
    [
        (ACCOUNT, '<input type="password"><button>Next</button>', "auth_required", False),
        (ACCOUNT, "<p>Loading your account</p>", "network_error", True),
        (
            "https://school.example.test/accounts.google.com",
            "Temporary sign-in service outage",
            "network_error",
            True,
        ),
    ],
)
async def test_only_real_authentication_failure_clears_session(
    mailbox, monkeypatch, target, body, error_code, authorized
):
    adapter, context, page = mailbox
    redirect_navigation(page, monkeypatch, target)
    await context.route(target, lambda route: route.fulfill(body=body, content_type="text/html"))
    await adapter.sync()
    state = adapter.db.state("gmail")
    assert state["authorized"] is authorized
    assert state["error_code"] == error_code
    assert state["last_sync"] == "old"
    cached = adapter.store.get("gmail:cached")
    assert cached["starred"] and cached["deleted"]


async def test_continued_session_still_checks_mailbox_identity(mailbox, monkeypatch):
    adapter, context, page = mailbox
    redirect_navigation(page, monkeypatch, ACCOUNT)
    await context.route(
        ACCOUNT,
        lambda route: route.fulfill(
            body=f'<script>setTimeout(() => location.href = "{INBOX}", 100)</script>',
            content_type="text/html",
        ),
    )
    await context.route(
        "https://mail.google.com/**",
        lambda route: route.fulfill(
            body=MAILBOX.replace("student@example.test", "different@example.test"),
            content_type="text/html",
        ),
    )
    await adapter.sync()
    state = adapter.db.state("gmail")
    assert state["error_code"] == "auth_required"
    assert state["account"] == "student@example.test" and state["last_sync"] == "old"


async def test_finish_login_can_resume_an_existing_account_tab(mailbox, monkeypatch):
    adapter, context, page = mailbox
    await context.route(
        ACCOUNT,
        lambda route: route.fulfill(body="<p>Resuming session</p>", content_type="text/html"),
    )
    await page.goto(ACCOUNT)
    adapter.browser.interactive = context
    monkeypatch.setattr(adapter, "spawn", lambda: None)
    await adapter.finish_login()
    assert adapter.db.state("gmail")["authorized"]
    adapter.browser.close_login.assert_awaited_once()


@pytest.mark.parametrize(
    "url",
    [
        "https://mail.google.com.evil.test/mail/u/0/",
        "https://mail.google.com:8443/mail/u/0/",
        "http://mail.google.com/mail/u/0/",
        "https://mail.google.com/other",
        "https://example.test/mail.google.com/mail/u/0/",
    ],
)
def test_inbox_readiness_requires_exact_mail_origin(url):
    assert not gmail_url(url)
