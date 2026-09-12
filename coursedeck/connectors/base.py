from abc import ABC, abstractmethod

from ..domain import SyncResult
from .capabilities import connector_capabilities, reading_capabilities


class Connector(ABC):
    key: str
    display_name: str
    description: str = ""
    manual_login = False

    @property
    def reading_capabilities(self):
        active = getattr(self, "active", None)
        if active is not None and active is not self:
            return connector_capabilities(active, self.key)
        return reading_capabilities(self.key)

    @property
    def configuration_fields(self) -> list[dict]:
        return []

    @property
    def configuration_values(self) -> dict:
        return {}

    @abstractmethod
    def connection_status(self) -> str: ...

    @abstractmethod
    async def connect(self, callback_url: str | None = None) -> dict: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def sync(self) -> SyncResult: ...

    async def close(self) -> None:
        return None

    async def configure(self, config: dict) -> None:
        raise ValueError("This source has no configuration")

    async def finish_login(self) -> dict:
        raise ValueError("This source does not use browser login")

    async def authorization_callback(self, params: dict) -> None:
        raise ValueError("This source does not use OAuth callbacks")
