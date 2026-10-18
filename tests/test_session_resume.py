import asyncio
import json
from time import monotonic
from types import SimpleNamespace

import pytest
from playwright.async_api import Locator, async_playwright

from coursedeck.session_resume import login_prompt_visible, resume_session, session_handoff_reason

READY = "https://classroom.google.com/u/0/h"
GOOGLE_CHOOSER = "https://accounts.google.com/AccountChooser"
GOOGLE_IDENTIFIER = "https://accounts.google.com/v3/signin/identifier"
MICROSOFT_IDENTIFIER = "https://login.microsoftonline.com/common/login"
MAILBOX = "student@example.edu"


@pytest.fixture
async def auth_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        pages = {}
        clicks = []
        await page.expose_function("recordClick", lambda label: clicks.append(label))

        async def route(request):
            await request.fulfill(
                content_type="text/html",
                body=pages.get(request.request.url, "<main>Coursework</main>"),
            )

        # Every request, including unintended destinations, stays in this fixture.
        await context.route("**/*", route)
        try:
            yield page, pages, clicks
        finally:
            await browser.close()


def action(label="next", *, navigate=True):
    destination = f"location.href={json.dumps(READY)};" if navigate else ""
    return f"await window.recordClick({json.dumps(label)}); {destination}"


def chooser(accounts):
    return "".join(
        f'<button data-identifier="{account}" '
        f"onclick='(async () => {{{action(account)}}})()'>{account}</button>"
        for account in accounts
    )


def identifier(provider="google", *, value=MAILBOX, navigate=True):
    handler = f"(async () => {{{action(navigate=navigate)}}})()"
    if provider == "google":
        return (
            f'<input type="email" name="identifier" value="{value}">'
            f"<div id=\"identifierNext\"><button onclick='{handler}'>Next</button></div>"
        )
    return (
        f'<input name="loginfmt" value="{value}">'
        f"<button id=\"idSIButton9\" onclick='{handler}'>Next</button>"
    )


async def resume(page, expected_account=None, timeout_ms=650):
    return await resume_session(
        page,
        lambda url: url == READY,
        expected_account=expected_account,
        timeout_ms=timeout_ms,
    )


@pytest.mark.parametrize(
    ("accounts", "expected", "chosen"),
    [
        ([MAILBOX], None, MAILBOX),
        (["other@example.edu", MAILBOX], MAILBOX.upper(), MAILBOX),
        (["other@example.edu", MAILBOX], None, None),
        (["other@example.edu"], MAILBOX, None),
    ],
)
async def test_google_account_choice_is_unambiguous(auth_page, accounts, expected, chosen):
    page, pages, clicks = auth_page
    # Google's real chooser includes a non-challenge iframe alongside the account.
    pages[GOOGLE_CHOOSER] = chooser(accounts) + '<iframe src="about:blank"></iframe>'
    await page.goto(GOOGLE_CHOOSER)
    assert await resume(page, expected) is (chosen is not None)
    assert clicks == ([chosen] if chosen else [])


@pytest.mark.parametrize(
    ("url", "provider"),
    [(GOOGLE_IDENTIFIER, "google"), (MICROSOFT_IDENTIFIER, "microsoft")],
)
async def test_prefilled_identifier_continues_without_entering_credentials(
    auth_page, url, provider
):
    page, pages, clicks = auth_page
    pages[url] = identifier(provider)
    await page.goto(url)
    assert await resume(page, MAILBOX.upper())
    assert clicks == ["next"]


@pytest.mark.parametrize("value", ["", "not-an-account", "different@example.edu"])
async def test_empty_or_wrong_prefilled_mailbox_is_not_submitted(auth_page, value):
    page, pages, clicks = auth_page
    pages[GOOGLE_IDENTIFIER] = identifier(value=value)
    await page.goto(GOOGLE_IDENTIFIER)
    assert not await resume(page, MAILBOX)
    assert clicks == []


@pytest.mark.parametrize(
    "challenge",
    [
        '<input type="password" value="synthetic-saved-password">',
        '<input autocomplete="one-time-code">',
        '<input name="otc">',
        '<input name="code">',
        '<input type="tel">',
        '<input type="number">',
        '<input type="checkbox">',
        '<iframe src="https://www.google.com/recaptcha/challenge"></iframe>',
    ],
)
async def test_visible_challenge_blocks_even_recognized_next(auth_page, challenge):
    page, pages, clicks = auth_page
    pages[GOOGLE_IDENTIFIER] = identifier() + challenge
    await page.goto(GOOGLE_IDENTIFIER)
    assert not await resume(page)
    assert clicks == []


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://accounts.google.com/signin/oauth/consent", "google"),
        ("https://accounts.google.com/v3/signin/challenge/pwd", "google"),
        ("https://login.microsoftonline.com/common/Consent/Set", "microsoft"),
        ("https://login.microsoftonline.com/kmsi", "microsoft"),
        ("https://login.microsoftonline.com/common/logout", "microsoft"),
        ("https://classroom.google.com/assignment/next", "google"),
        ("https://accounts.google.com.example.org/v3/signin/identifier", "google"),
        ("http://accounts.google.com/v3/signin/identifier", "google"),
        ("https://accounts.google.com:8443/v3/signin/identifier", "google"),
    ],
)
async def test_other_pages_never_click_next(auth_page, url, provider):
    page, pages, clicks = auth_page
    pages[url] = identifier(provider)
    await page.goto(url)
    assert not await resume(page)
    assert clicks == []


async def test_stay_signed_in_button_alone_is_not_username_step(auth_page):
    page, pages, clicks = auth_page
    pages[MICROSOFT_IDENTIFIER] = (
        '<h1>Stay signed in?</h1><button id="idSIButton9" '
        f"onclick='(async () => {{{action()}}})()'>Continue</button>"
    )
    await page.goto(MICROSOFT_IDENTIFIER)
    assert not await resume(page)
    assert clicks == []


async def test_unknown_sso_page_can_finish_its_own_redirect(auth_page):
    page, pages, clicks = auth_page
    url = "https://sso.example.edu/resume"
    pages[url] = f"<script>setTimeout(() => location.href = {json.dumps(READY)}, 100)</script>"
    await page.goto(url)
    assert await resume(page)
    assert clicks == []


async def test_repeated_next_is_attempted_once_and_exits_within_budget(auth_page):
    page, pages, clicks = auth_page
    pages[GOOGLE_IDENTIFIER] = identifier(navigate=False)
    await page.goto(GOOGLE_IDENTIFIER)
    start = monotonic()
    assert not await resume(page, timeout_ms=700)
    assert monotonic() - start < 2
    assert clicks == ["next"]


async def test_dom_replacement_cannot_extend_recovery_deadline(monkeypatch):
    cancelled = asyncio.Event()

    async def pending_locator(*args):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    monkeypatch.setattr("coursedeck.session_resume._continuation", pending_locator)
    page = SimpleNamespace(url=GOOGLE_CHOOSER)
    assert not await asyncio.wait_for(resume(page, timeout_ms=20), timeout=1)
    assert cancelled.is_set()


@pytest.mark.parametrize("expected", [MAILBOX, "other@example.edu"])
async def test_google_confirm_existing_identity_without_input(auth_page, expected):
    page, pages, clicks = auth_page
    url = "https://accounts.google.com/v3/signin/confirmidentifier"
    pages[url] = (
        "<h1>Verify it’s you</h1>"
        f'<div data-profile-identifier data-email="{MAILBOX}">{MAILBOX}</div>'
        '<div id="identifierNext">'
        f"<button onclick='(async () => {{{action()}}})()'>Next</button></div>"
    )
    await page.goto(url)
    assert await login_prompt_visible(page)
    assert await resume(page, expected) is (expected == MAILBOX)
    assert clicks == (["next"] if expected == MAILBOX else [])


@pytest.mark.parametrize("method", ["get_attribute", "input_value", "is_enabled"])
async def test_navigation_during_probe_rechecks_ready_without_clicking(
    auth_page, monkeypatch, method
):
    page, pages, clicks = auth_page
    entry = GOOGLE_CHOOSER if method == "get_attribute" else GOOGLE_IDENTIFIER
    pages[entry] = chooser([MAILBOX]) if method == "get_attribute" else identifier()
    await page.goto(entry)
    original = getattr(Locator, method)
    moved = False

    async def replaced(locator, *args, **kwargs):
        nonlocal moved
        if not moved:
            moved = True
            await page.goto(READY)
        return await original(locator, *args, **kwargs)

    monkeypatch.setattr(Locator, method, replaced)
    assert await resume(page, timeout_ms=1500)
    assert moved and not clicks


async def test_delayed_next_redirect_never_reprobes_enabled_control(auth_page, monkeypatch):
    page, pages, clicks = auth_page
    pages[GOOGLE_IDENTIFIER] = identifier(navigate=False).replace(
        'await window.recordClick("next");',
        f'await window.recordClick("next");setTimeout(()=>location.href={json.dumps(READY)},450);',
    )
    await page.goto(GOOGLE_IDENTIFIER)
    original = Locator.is_enabled
    probes = 0

    async def enabled(locator, *args, **kwargs):
        nonlocal probes
        probes += 1
        return await original(locator, *args, **kwargs)

    monkeypatch.setattr(Locator, "is_enabled", enabled)
    assert await resume(page, timeout_ms=1500)
    assert clicks == ["next"] and probes == 1


@pytest.mark.parametrize(
    "challenge,reason",
    [
        ('<input name="totpPin" value="123456">', "mfa_required"),
        ('<input name="idvPin" value="123456">', "mfa_required"),
        ('<input name="backupCodePin" value="123456">', "mfa_required"),
        ("<button>Allow</button>", "permission_required"),
        ("<button>I agree</button>", "permission_required"),
        ("<h1>Updated terms of service</h1>", "permission_required"),
        ("<h1>Permissions requested</h1>", "permission_required"),
        ('<input type="password" value="fixture-secret">', "credentials_required"),
    ],
)
async def test_confirm_identifier_challenges_require_user_without_secret_reads(
    auth_page, monkeypatch, challenge, reason
):
    page, pages, clicks = auth_page
    url = "https://accounts.google.com/v3/signin/confirmidentifier"
    pages[url] = (
        f'<div data-profile-identifier data-email="{MAILBOX}">{MAILBOX}</div>'
        '<div id="identifierNext">'
        f"<button onclick='(async () => {{{action()}}})()'>Next</button></div>" + challenge
    )
    await page.goto(url)

    async def no_secret_reads(*args, **kwargs):
        pytest.fail("Password and verification inputs must not be read")

    monkeypatch.setattr(Locator, "input_value", no_secret_reads)
    assert await session_handoff_reason(page) == reason
    assert not await resume(page)
    assert not clicks


@pytest.mark.parametrize(
    "url,html,reason",
    [
        (GOOGLE_IDENTIFIER, identifier(), None),
        (GOOGLE_IDENTIFIER, identifier(value=""), "credentials_required"),
        (GOOGLE_CHOOSER, chooser([MAILBOX]), None),
        (GOOGLE_CHOOSER, chooser([MAILBOX, "other@example.edu"]), "account_selection_required"),
        ("https://school.example/login", '<input autocomplete="username">', "credentials_required"),
        ("https://school.example/login", '<input name="idvPin">', "mfa_required"),
        ("https://school.example/resume", "Loading", None),
    ],
)
async def test_handoff_distinguishes_resumable_steps_and_human_challenges(
    auth_page, url, html, reason
):
    page, pages, clicks = auth_page
    pages[url] = html
    await page.goto(url)
    assert await session_handoff_reason(page) == reason
    assert not clicks


async def test_confirm_identifier_is_not_a_handoff_without_challenge(auth_page):
    page, pages, clicks = auth_page
    url = "https://accounts.google.com/v3/signin/confirmidentifier"
    pages[url] = (
        f'<div data-profile-identifier data-email="{MAILBOX}"></div>'
        '<div id="identifierNext"><button>Next</button></div>'
    )
    await page.goto(url)
    # The account marker must be visible, as on Google's confirmation page.
    await page.locator("[data-email]").evaluate("e => e.textContent = 'Student'")
    assert await session_handoff_reason(page, expected_account=MAILBOX) is None
    assert await session_handoff_reason(page, expected_account="other@example.edu") == (
        "account_selection_required"
    )
    assert not clicks


async def test_cancellation_during_short_probe_propagates(monkeypatch):
    started, finished = asyncio.Event(), asyncio.Event()

    async def pending(*args):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()

    monkeypatch.setattr("coursedeck.session_resume._continuation", pending)
    task = asyncio.create_task(resume(SimpleNamespace(url=GOOGLE_CHOOSER), timeout_ms=5000))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_recovery_never_exceeds_three_distinct_steps(auth_page):
    page, pages, clicks = auth_page
    pages[GOOGLE_IDENTIFIER] = (
        '<input name="identifier" type="email" value="step0@example.edu">'
        '<div id="identifierNext"><button onclick="'
        "window.recordClick('next'); window.step = (window.step || 0) + 1;"
        "document.querySelector('input').value = 'step' + window.step + '@example.edu';"
        '">Next</button></div>'
    )
    await page.goto(GOOGLE_IDENTIFIER)
    assert not await resume(page, timeout_ms=1500)
    assert clicks == ["next", "next", "next"]
