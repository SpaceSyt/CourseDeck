"""Resume saved sign-in sessions without entering credentials or granting access."""

import asyncio
import re
from collections.abc import Callable
from time import monotonic
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

NEXT = re.compile(r"^(?:Next|Continue|下一步|继续)$", re.I)
GOOGLE_ACCOUNT_PATHS = {"/AccountChooser", "/v3/signin/accountchooser", "/signin/v2/identifier"}
GOOGLE_IDENTIFIER_PATHS = {"/v3/signin/identifier", "/signin/v2/identifier", "/ServiceLogin"}


async def _continuation(page, expected_account):
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        return None
    host, path = parsed.hostname, parsed.path.rstrip("/")
    if host not in {"accounts.google.com", "login.microsoftonline.com"}:
        return None
    # A visible challenge always wins over a continuation control on the same page.
    if await page.locator(
        'input[type="password"]:visible, input[type="tel"]:visible, '
        'input[type="number"]:visible, input[autocomplete="one-time-code"]:visible, '
        'input[name="otc"]:visible, input[name="code"]:visible, '
        'input[type="checkbox"]:visible, iframe[src*="recaptcha"]:visible, '
        'iframe[src*="hcaptcha"]:visible, iframe[src*="duosecurity"]:visible'
    ).count():
        return None
    if host == "accounts.google.com" and path in GOOGLE_ACCOUNT_PATHS:
        choices = page.locator("[data-identifier]")
        candidates = []
        for index in range(await choices.count()):
            choice = choices.nth(index)
            identity = (await choice.get_attribute("data-identifier") or "").strip().casefold()
            if await choice.is_visible() and "@" in identity:
                if expected_account is None or identity == expected_account.casefold():
                    candidates.append((choice, identity))
        # Never guess between multiple accounts, or switch away from a linked mailbox.
        if len(candidates) == 1:
            choice, identity = candidates[0]
            return choice, (host, path, "account", identity)
    if host == "accounts.google.com":
        if path == "/v3/signin/confirmidentifier":
            account = page.locator("[data-profile-identifier][data-email]:visible")
            button = page.locator("#identifierNext").get_by_role("button", name=NEXT)
            if await account.count() != 1 or await button.count() != 1:
                return None
            identity = (await account.get_attribute("data-email") or "").strip().casefold()
            if not identity or "@" not in identity:
                return None
            if expected_account is not None and identity != expected_account.casefold():
                return None
            if await button.is_visible() and await button.is_enabled():
                return button, (host, path, "confirm_identifier", identity)
            return None
        if path not in GOOGLE_IDENTIFIER_PATHS:
            return None
        identifier = page.locator('input[type="email"][name="identifier"]:visible')
        button = page.locator("#identifierNext").get_by_role("button", name=NEXT)
    else:
        # These controls identify Microsoft's username step; idSIButton9 alone is
        # also used for password submission and "Stay signed in", so is insufficient.
        if any(word in path.lower() for word in ("consent", "kmsi", "logout")):
            return None
        identifier = page.locator('input[name="loginfmt"]:visible')
        button = page.locator("#idSIButton9").and_(page.get_by_role("button", name=NEXT))
    if await identifier.count() != 1 or await button.count() != 1:
        return None
    identity = (await identifier.input_value()).strip().casefold()
    if not identity or "@" not in identity:
        return None
    if expected_account is not None and identity != expected_account.casefold():
        return None
    if not await button.is_visible() or not await button.is_enabled():
        return None
    return button, (host, path, "identifier", identity)


async def resume_session(
    page,
    is_ready: Callable[[str], bool],
    *,
    expected_account: str | None = None,
    timeout_ms: int = 20000,
) -> bool:
    """Wait for SSO and advance recognized account steps; callers still validate data.

    Unknown pages are left untouched. A bounded attempt does not repeatedly submit
    the same step, and never reads saved passwords or OTPs, or logs authentication URLs.
    """
    deadline = monotonic() + timeout_ms / 1000
    attempted = set()
    while True:
        if is_ready(page.url):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(remaining):
                continuation = await _continuation(page, expected_account)
            if continuation is not None and len(attempted) < 3:
                control, identity = continuation
                if identity not in attempted:
                    attempted.add(identity)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return False
                    await control.click(timeout=max(1, min(1500, int(remaining * 1000))))
        except TimeoutError:
            return False
        except BrowserError:
            # Redirects can replace the execution context between inspection and click.
            if page.is_closed():
                raise
        await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))


async def login_prompt_visible(page) -> bool:
    """Distinguish a visible sign-in challenge from an unfinished SSO redirect."""
    parsed = urlsplit(page.url)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.hostname not in {"accounts.google.com", "login.microsoftonline.com"}
    ):
        return False
    return bool(
        await page.locator(
            'input[type="password"]:visible, input[name="identifier"]:visible, '
            'input[name="loginfmt"]:visible, input[type="tel"]:visible, '
            'input[type="number"]:visible, input[autocomplete="one-time-code"]:visible, '
            'input[name="otc"]:visible, input[name="code"]:visible, '
            'input[name="totpPin"]:visible, input[name="idvPin"]:visible, '
            'input[name="backupCodePin"]:visible, [data-identifier]:visible, '
            "[data-profile-identifier][data-email]:visible, "
            '#tilesHolder [role="button"]:visible, '
            'iframe[src*="/recaptcha/"]:visible'
        ).count()
    )
