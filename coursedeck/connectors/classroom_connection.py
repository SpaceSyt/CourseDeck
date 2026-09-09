from ..browser import BrowserManager
from ..credentials import CredentialStore
from ..db import Database
from .base import Connector
from .classroom import ClassroomConnector as ClassroomAPIConnector
from .classroom_browser import ClassroomBrowserConnector


class ClassroomConnector(Connector):
    key = "google_classroom"
    display_name = "Google Classroom"

    def __init__(self, db: Database, vault: CredentialStore, browser: BrowserManager):
        self.db = db
        self.api = ClassroomAPIConnector(db, vault)
        self.browser = ClassroomBrowserConnector(db, browser)

    @property
    def mode(self):
        state = self.db.state(self.key)
        return state.get("transport", "official_api" if state.get("configured") else "browser")

    @property
    def active(self):
        return self.api if self.mode == "official_api" else self.browser

    @property
    def description(self):
        return self.active.description

    @property
    def manual_login(self):
        return self.active.manual_login

    @property
    def configuration_fields(self):
        return [
            {
                "key": "transport",
                "label": "Connection method",
                "type": "select",
                "options": ["browser", "official_api"],
                "help": "Browser login needs no Cloud project. Official API is optional.",
            },
            *self.active.configuration_fields,
        ]

    @property
    def configuration_values(self):
        return {"transport": self.mode, **self.active.configuration_values}

    def connection_status(self):
        return self.active.connection_status()

    async def configure(self, config):
        values = dict(config)
        mode = values.pop("transport", self.mode)
        if mode not in {"browser", "official_api"}:
            raise ValueError("Unknown connection method")
        if mode != self.mode:
            # A two-step switch lets the generic UI show the new method's fields.
            # Reject credentials in this step to avoid silently dropping them.
            old_fields = {field["key"] for field in self.active.configuration_fields}
            if values.keys() - old_fields:
                raise ValueError("Save the connection method before configuring credentials")
            self.api.pending = None
            await self.browser.close()
            self.db.update_state(
                self.key, transport=mode, authorized=False, last_outcome=None, warnings=[]
            )
            return
        if values:
            await self.active.configure(values)
        self.db.update_state(self.key, transport=mode)

    async def connect(self, callback_url=None):
        return await self.active.connect(callback_url)

    async def disconnect(self):
        # Explicit disconnect removes both saved login sessions, including a
        # previously selected method. Cached assignments and notes stay local.
        if self.db.state(self.key).get("configured"):
            await self.api.disconnect()
        await self.browser.disconnect()

    async def finish_login(self):
        return await self.active.finish_login()

    async def authorization_callback(self, params):
        if self.mode != "official_api":
            raise ValueError("OAuth is not the selected connection method")
        await self.api.authorization_callback(params)

    async def sync(self):
        return await self.active.sync()

    async def close(self):
        await self.browser.close()
