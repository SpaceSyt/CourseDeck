from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

from ..browser import BrowserManager
from ..db import Database
from ..domain import Outcome, Settings
from .base import Connector
from .http import TransportError


def secure_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A valid HTTPS source URL is required")
    if parsed.query or parsed.fragment:
        raise ValueError("Do not include session tokens or query parameters in the base URL")
    return value.rstrip("/")


async def session_html(context: BrowserContext, url: str) -> str:
    response = await context.request.get(url, timeout=45000)
    if response.status == 429:
        raise TransportError(Outcome.RATE_LIMITED, "Rate limited. Wait before retrying.")
    if response.status >= 500:
        raise TransportError(Outcome.NETWORK_ERROR, "Source temporarily unavailable.")
    body = await response.text()
    if (
        response.status in {401, 403}
        or is_login(body)
        or urlparse(response.url).hostname != urlparse(url).hostname
    ):
        raise TransportError(Outcome.AUTH_REQUIRED, "Session expired. Reconnect this source.")
    if response.status != 200:
        raise TransportError(
            Outcome.PARSE_ERROR, "Unexpected source page. Check connector configuration."
        )
    return body


def is_login(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one('input[type="password"]') is not None


class BrowserConnector(Connector):
    default_url = ""
    login_path = ""
    manual_login = True

    @property
    def configuration_fields(self):
        return [
            {
                "key": "base_url",
                "label": "School / platform URL",
                "type": "url",
                "placeholder": self.default_url or "https://school.brightspace.com",
            },
            {
                "key": "timezone",
                "label": "Source timezone",
                "type": "text",
                "placeholder": "America/New_York",
                "help": "Use the timezone displayed by the source for dates without an offset.",
            },
        ]

    @property
    def configuration_values(self):
        return {"base_url": self.base_url, **self.config}

    def __init__(self, db: Database, browser: BrowserManager):
        self.db, self.browser = db, browser

    @property
    def config(self):
        return self.db.state(self.key).get("config", {})

    @property
    def base_url(self):
        return self.config.get("base_url", self.default_url)

    def connection_status(self):
        if self.browser.interactive is not None:
            return "login_pending"
        if not self.base_url:
            return "not_configured"
        if self.db.state(self.key).get("authorized") and self.browser.exists():
            return "connected"
        return "not_connected"

    async def configure(self, config: dict):
        allowed = {"base_url", "timezone"}
        if config.keys() - allowed:
            raise ValueError("Unrecognized browser configuration field")
        merged = self.config | config
        if "base_url" in merged:
            merged["base_url"] = secure_url(merged["base_url"])
        if merged.get("timezone"):
            Settings(timezone=merged["timezone"])
        if merged.get("base_url", self.default_url) != self.base_url:
            self.db.update_state(self.key, authorized=False)
        self.db.update_state(self.key, config=merged)

    async def connect(self, callback_url: str | None = None):
        if not self.base_url:
            raise ValueError("Configure your school's HTTPS source URL first")
        await self.browser.login(self.base_url + self.login_path, self.config.get("timezone"))
        return {"message": "Complete SSO / 2FA in the dedicated browser, then click Finish login."}

    async def validate_session(self, context: BrowserContext) -> bool:
        raise NotImplementedError

    async def finish_login(self):
        context = self.browser.interactive
        if context is None:
            raise ValueError("Open the login browser first")
        if not await self.validate_session(context):
            return {"message": "Login could not be confirmed. Finish SSO in the browser and retry."}
        await self.browser.close_login()
        self.db.update_state(self.key, authorized=True, last_outcome=None, warnings=[])
        return {"message": "Browser session saved. Syncing your coursework."}

    async def disconnect(self):
        await self.browser.reset()
        self.db.update_state(self.key, authorized=False, last_outcome=None, warnings=[])

    async def close(self):
        await self.browser.close()
