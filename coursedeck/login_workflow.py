"""Bounded WebAssign login steps; credentials remain inside the visible browser."""

import asyncio
import re
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from pydantic import BaseModel, ConfigDict, Field, field_validator

HOSTS = {
    "www.webassign.net",
    "webassign.net",
    "account.cengage.com",
    "auth.cengage.com",
    "login.cengage.com",
}
LOGIN_BUTTON = re.compile(r"^(?:sign in|log in|login|next|continue|log in to webassign)$", re.I)
USERNAME = 'input[autocomplete="username"]:visible, input[type="email"]:visible'
MFA = (
    'input[autocomplete="one-time-code"]:visible, input[name="otc"]:visible, '
    'input[name="code"]:visible, input[name="totpPin"]:visible, '
    'input[name="idvPin"]:visible, input[name="backupCodePin"]:visible, '
    'input[type="tel"]:visible, input[type="number"]:visible, '
    'iframe[src*="duosecurity"]:visible, iframe[src*="recaptcha"]:visible, '
    'iframe[src*="hcaptcha"]:visible'
)


def login_route(path):
    return (
        path.startswith("/")
        and len(path) <= 200
        and not any(x in path for x in ("?", "#", "\\", "%"))
        and bool(re.search(r"(?:^|/)(?:login|signin|sign-in|sso|oauth|auth)(?:[/.]|$)", path, re.I))
        and not re.search(
            r"logout|consent|permissions|assignment|submission|quiz|grade", path, re.I
        )
    )


class LoginStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    path: str = Field(min_length=1, max_length=200)
    action: str = Field(pattern=r"^(wait|continue|autofill_submit|require_user)$")
    selector: str = Field(default="button, input[type=submit]", min_length=1, max_length=200)

    @field_validator("host")
    @classmethod
    def known_host(cls, value):
        if value not in HOSTS:
            raise ValueError("Choose a supported WebAssign or Cengage login host")
        return value

    @field_validator("path")
    @classmethod
    def public_path(cls, value):
        if not login_route(value):
            raise ValueError("Workflow steps are restricted to login routes")
        return value

    @field_validator("selector")
    @classmethod
    def locator_only(cls, value):
        # Recipes select controls, never their credential values or executable code.
        if re.search(r"value\s*[=~|^$*]|javascript:|>>|secret|token|password\s*=", value, re.I):
            raise ValueError("Login selectors must not contain credential values")
        return value


class LoginWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_url: str = "https://www.webassign.net/login.html"
    timeout_seconds: int = Field(default=25, ge=1, le=90, strict=True)
    steps: list[LoginStep] = Field(default_factory=list, max_length=8)

    @field_validator("entry_url")
    @classmethod
    def login_entry(cls, value):
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or p.hostname not in HOSTS
            or p.port not in (None, 443)
            or p.username
            or p.password
            or p.query
            or p.fragment
            or not re.search(r"(?:login|signin|sign-in)(?:[/.]|$)", p.path, re.I)
        ):
            raise ValueError("Use a public HTTPS login URL without credentials or parameters")
        return value


@dataclass(frozen=True)
class LoginProgress:
    category: str
    message: str
    steps_completed: int = 0


MESSAGES = {
    "ready": "Login verified. Coursework can be read.",
    "waiting_mfa": "Complete verification in the source browser, then click Finish login.",
    "waiting_credentials": (
        "Use your password manager in the source browser, then click Finish login."
    ),
    "waiting_permission": (
        "Review the permission request in the source browser. No permission was accepted."
    ),
    "waiting_user": "Continue in the source browser, then click Finish login.",
    "unknown_redirect": "Sign-in reached an unrecognized page. Continue in the source browser.",
    "redirect_timeout": (
        "Sign-in did not reach readable coursework. Check the source browser and retry."
    ),
}


async def run_login_workflow(page, workflow, ready, *, interactive=False):
    """Only click recognized login controls; never read password/OTP input values.

    `ready` must verify protected source content as well as its destination.
    Password-manager submission is available only in an explicit visible login.
    """
    deadline = monotonic() + workflow.timeout_seconds
    attempted = set()
    completed_steps = set()

    def result(category):
        return LoginProgress(category, MESSAGES[category], len(attempted))

    def probe_timeout():
        # A redirect can remove a locator between count() and an auto-waiting probe.
        return min(250, max(1, int((deadline - monotonic()) * 1000)))

    async def advance():
        while monotonic() < deadline:
            parsed = urlsplit(page.url)
            if parsed.scheme != "https" or parsed.port not in (None, 443):
                return result("unknown_redirect")
            # Challenges are detected on external school IdPs too, but never acted on.
            if await page.locator(MFA).count():
                return result("waiting_mfa")
            if await page.get_by_text(
                re.compile(
                    r"^(?:verify your identity|check your phone|approve (?:the )?sign.in|"
                    r"two.factor authentication|multi.factor authentication)$",
                    re.I,
                )
            ).count():
                return result("waiting_mfa")
            if any(x in parsed.path.lower() for x in ("consent", "permissions")):
                return result("waiting_permission")
            if await page.get_by_role(
                "button",
                name=re.compile(
                    r"^(?:allow|accept|approve|grant access|agree|yes,? stay signed in)$", re.I
                ),
            ).count():
                return result("waiting_permission")
            if await page.get_by_text(
                re.compile(
                    r"permissions requested|wants (?:permission|access) to|allow this app to", re.I
                )
            ).count():
                return result("waiting_permission")
            if await ready(page):
                return result("ready")
            if parsed.hostname not in HOSTS:
                if await page.locator(f'input[type="password"]:visible, {USERNAME}').count():
                    return result("waiting_credentials")
                # School SSO intermediates may redirect themselves; never click on them.
                await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))
                continue
            matching = [
                (i, s)
                for i, s in enumerate(workflow.steps)
                if i not in completed_steps and s.host == parsed.hostname and s.path == parsed.path
            ]
            step_index, step = matching[0] if matching else (None, None)
            configured_route = any(
                s.host == parsed.hostname and s.path == parsed.path for s in workflow.steps
            )
            if step and step.action == "require_user":
                return result("waiting_user")
            password = page.locator('input[type="password"]:visible')
            has_password = bool(await password.count())
            username = page.locator(USERNAME)
            if not has_password and login_route(parsed.path) and await username.count():
                if not interactive or (step and step.action != "autofill_submit"):
                    return result("waiting_credentials")
                if await username.count() != 1 or not await username.evaluate(
                    "e => e.matches(':autofill') || e.matches(':-webkit-autofill')",
                    timeout=probe_timeout(),
                ):
                    return result("waiting_credentials")
            if has_password:
                if not interactive or (step and step.action != "autofill_submit"):
                    return result("waiting_credentials")
                # The browser supplies only its autofill state, never the saved value.
                if await password.count() != 1 or not await password.evaluate(
                    "e => e.matches(':autofill') || e.matches(':-webkit-autofill')",
                    timeout=probe_timeout(),
                ):
                    return result("waiting_credentials")
            if step and step.action == "wait":
                await page.locator(step.selector).first.wait_for(
                    state="visible", timeout=max(1, int((deadline - monotonic()) * 1000))
                )
                completed_steps.add(step_index)
                continue
            # No generic Next click outside an identified login route.
            if login_route(parsed.path) and (step is not None or not configured_route):
                key = (
                    parsed.hostname,
                    parsed.path,
                    "password" if has_password else "continue",
                    step_index,
                )
                if key in attempted:
                    await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))
                    continue
                candidates = page.get_by_role("button", name=LOGIN_BUTTON)
                if step:
                    candidates = candidates.and_(page.locator(step.selector))
                if await candidates.count() == 1:
                    control = candidates.first
                    if await control.is_visible() and await control.is_enabled(
                        timeout=probe_timeout()
                    ):
                        if await page.locator('input[type="checkbox"]:visible').count():
                            return result("waiting_user")
                        # Never send browser-autofilled credentials to a different form origin.
                        action = await control.evaluate(
                            "e => e.form?.action || location.href", timeout=probe_timeout()
                        )
                        action_url = urlsplit(action)
                        if (
                            action_url.scheme != "https"
                            or action_url.hostname != parsed.hostname
                            or action_url.port not in (None, 443)
                            or action_url.username
                            or action_url.password
                            or not login_route(action_url.path)
                        ):
                            return result("waiting_user")
                        if len(attempted) >= 8:
                            return result("waiting_user")
                        attempted.add(key)
                        if step_index is not None:
                            completed_steps.add(step_index)
                        await control.click(
                            timeout=min(1500, max(1, int((deadline - monotonic()) * 1000)))
                        )
            await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))
        return result(
            "unknown_redirect" if urlsplit(page.url).hostname not in HOSTS else "redirect_timeout"
        )

    while monotonic() < deadline:
        try:
            async with asyncio.timeout(max(0.001, deadline - monotonic())):
                return await advance()
        except TimeoutError:
            return result("redirect_timeout")
        except BrowserError:
            if page.is_closed():
                return result("waiting_user")
            # SSO can replace a document between checking and clicking a control.
            await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))
    return result("redirect_timeout")
