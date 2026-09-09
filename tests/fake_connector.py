"""Synthetic connector for engine tests only; never registered in the application."""

from coursedeck.connectors.base import Connector
from coursedeck.domain import Course, Outcome, SyncResult, Task


class FakeConnector(Connector):
    key = "fixture"
    display_name = "Test source"

    def __init__(self, db):
        self.db = db
        self.enabled = False

    def connection_status(self):
        return "connected" if self.enabled else "not_connected"

    async def connect(self, callback_url=None):
        self.enabled = True
        return {}

    async def disconnect(self):
        self.enabled = False

    async def sync(self):
        if not self.enabled:
            return SyncResult(outcome=Outcome.AUTH_REQUIRED)
        return SyncResult(
            outcome=Outcome.SUCCESS,
            complete=True,
            courses=[Course(provider=self.key, external_id="course", name="Test course")],
            tasks=[
                Task(
                    provider=self.key,
                    course_external_id="course",
                    external_id=str(i),
                    title=f"Test task {i}",
                )
                for i in range(6)
            ],
        )
