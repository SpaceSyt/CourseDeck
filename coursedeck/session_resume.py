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
PROBE_TIMEOUT_MS = 250
MFA_INPUTS = (
    'input[type="tel"]:visible, input[type="number"]:visible, '
    'input[autocomplete="one-time-code"]:visible, input[name="otc"]:visible, '
    'input[name="code"]:visible, input[name="totpPin"]:visible, '
    'input[name="idvPin"]:visible, input[name="backupCodePin"]:visible, '
    'iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible, '
    'iframe[src*="duosecurity"]:visible'
)


async def _handoff_challenge(page):
    if await page.locator(MFA_INPUTS).count():
        return "mfa_required"
    if (
        await page.get_by_text(
            re.compile(
                r"^(?:verify your identity|check your phone|approve (?:the )?sign.in|"
                r"two.factor authentication|multi.factor authentication)$",
                re.I,
            )
        )
        .and_(page.locator(":visible"))
        .count()
    ):
        return "mfa_required"
    if any(word in urlsplit(page.url).path.lower() for word in ("consent", "permissions", "kmsi")):
        return "permission_required"
    if await page.get_by_role(
        "button",
        name=re.compile(
            r"^(?:allow|accept(?: all)?|approve|grant access|agree|i agree|accept (?:the )?terms|"
            r"agree and continue|yes,? stay signed in|同意|接受|允许|同意并继续)$",
            re.I,
        ),
    ).count():
        return "permission_required"
    if (
        await page.get_by_text(
            re.compile(
                r"permissions requested|wants (?:permission|access) to|allow this app to|"
                r"(?:new|updated) terms|accept (?:the )?terms|agree to (?:the )?terms|"
                r"stay signed in|新的条款|更新后的条款|同意条款",
                re.I,
            )
        )
        .and_(page.locator(":visible"))
        .count()
    ):
        return "permission_required"
    if await page.locator('input[type="checkbox"]:visible').count():
        return "permission_required"
    if await page.locator('input[type="password"]:visible').count():
        return "credentials_required"
    return None


async def _continuation(page, expected_account, attempted=frozenset()):
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        return None
    host, path = parsed.hostname, parsed.path.rstrip("/")
    if host not in {"accounts.google.com", "login.microsoftonline.com"}:
        return None
    # A visible challenge always wins over a continuation control on the same page.
    if await _handoff_challenge(page):
        return None
    if host == "accounts.google.com" and path in GOOGLE_ACCOUNT_PATHS:
        choices = page.locator("[data-identifier]")
        candidates = []
        for index in range(await choices.count()):
            choice = choices.nth(index)
            identity = (
                (await choice.get_attribute("data-identifier", timeout=PROBE_TIMEOUT_MS) or "")
                .strip()
                .casefold()
            )
            if await choice.is_visible() and "@" in identity:
                if expected_account is None or identity == expected_account.casefold():
                    candidates.append((choice, identity))
        # Never guess between multiple accounts, or switch away from a linked mailbox.
        if len(candidates) == 1:
            choice, identity = candidates[0]
            key = (host, path, "account", identity)
            return (choice, key) if key not in attempted else None
    if host == "accounts.google.com":
        if path == "/v3/signin/confirmidentifier":
            account = page.locator("[data-profile-identifier][data-email]:visible")
            button = page.locator("#identifierNext").get_by_role("button", name=NEXT)
            if await account.count() != 1 or await button.count() != 1:
                return None
            identity = (
                (await account.get_attribute("data-email", timeout=PROBE_TIMEOUT_MS) or "")
                .strip()
                .casefold()
            )
            if not identity or "@" not in identity:
                return None
            if expected_account is not None and identity != expected_account.casefold():
                return None
            key = (host, path, "confirm_identifier", identity)
            if key in attempted:
                return None
            if await button.is_visible() and await button.is_enabled(timeout=PROBE_TIMEOUT_MS):
                return button, key
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
    identity = (await identifier.input_value(timeout=PROBE_TIMEOUT_MS)).strip().casefold()
    if not identity or "@" not in identity:
        return None
    if expected_account is not None and identity != expected_account.casefold():
        return None
    key = (host, path, "identifier", identity)
    if key in attempted:
        return None
    if not await button.is_visible() or not await button.is_enabled(timeout=PROBE_TIMEOUT_MS):
        return None
    return button, key


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
            async with asyncio.timeout(min(remaining, PROBE_TIMEOUT_MS / 1000)):
                continuation = await _continuation(page, expected_account, attempted)
            if continuation is not None and len(attempted) < 3:
                control, identity = continuation
                if identity not in attempted:
                    parsed = urlsplit(page.url)
                    if (parsed.hostname, parsed.path.rstrip("/")) != identity[:2]:
                        continue
                    async with asyncio.timeout(
                        min(max(0.001, deadline - monotonic()), PROBE_TIMEOUT_MS / 1000)
                    ):
                        if await _handoff_challenge(page):
                            continue
                    parsed = urlsplit(page.url)
                    if (
                        parsed.scheme != "https"
                        or parsed.port not in (None, 443)
                        or (parsed.hostname, parsed.path.rstrip("/")) != identity[:2]
                    ):
                        continue
                    attempted.add(identity)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return False
                    await control.click(timeout=max(1, min(1500, int(remaining * 1000))))
        except TimeoutError:
            # A short probe may outlive its old document; inspect the new URL next.
            pass
        except BrowserError:
            # Redirects can replace the execution context between inspection and click.
            if page.is_closed():
                raise
        await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))


async def session_handoff_reason(page, *, expected_account=None):
    """Classify explicit human steps; None also includes an unfinished redirect."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_MS / 1000):
            parsed = urlsplit(page.url)
            if parsed.scheme != "https" or parsed.port not in (None, 443):
                return None
            reason = await _handoff_challenge(page)
            if reason:
                return reason
            if await _continuation(page, expected_account) is not None:
                return None
            if await page.locator(
                "[data-identifier]:visible, [data-profile-identifier][data-email]:visible, "
                '#tilesHolder [role="button"]:visible'
            ).count():
                return "account_selection_required"
            if await page.locator(
                'input[name="identifier"]:visible, input[name="loginfmt"]:visible, '
                'input[type="email"]:visible, input[autocomplete="username"]:visible, '
                'input[name="username"]:visible, input[name="email"]:visible'
            ).count():
                return "credentials_required"
    except (TimeoutError, BrowserError):
        # A replacing document is not evidence that the saved login is invalid.
        pass
    return None


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
