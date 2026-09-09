import asyncio
from collections.abc import Coroutine

from .connectors.base import Connector
from .db import Database
from .domain import Outcome, SyncResult, now


class SyncEngine:
    def __init__(self, db: Database, connectors: list[Connector]):
        self.db = db
        self.connectors = {c.key: c for c in connectors}
        self.locks = {c.key: asyncio.Lock() for c in connectors}
        self.jobs: set[asyncio.Task] = set()
        self.running: set[str] = set()
        self.revision = 0

    def spawn(self, coroutine: Coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return job

    async def sync_one(self, key: str):
        if self.locks[key].locked():
            return
        async with self.locks[key]:
            self.running.add(key)
            self.revision += 1
            attempted = now().isoformat()
            self.db.update_state(key, last_attempted_sync=attempted)
            try:
                try:
                    result = await asyncio.wait_for(self.connectors[key].sync(), timeout=180)
                except TimeoutError:
                    result = SyncResult(
                        outcome=Outcome.NETWORK_ERROR,
                        warnings=["Sync timed out. Cached data is still available."],
                    )
                except Exception:
                    # Never persist exception strings that may contain URLs, cookies or tokens.
                    result = SyncResult(
                        outcome=Outcome.ERROR,
                        warnings=["Connector failed. See setup and parser guidance."],
                    )
                try:
                    self.db.apply(key, result, attempted)
                except Exception as exc:
                    # Rollback prevents half-applied snapshots. Store only a safe error category.
                    result = SyncResult(
                        outcome=Outcome.PARSE_ERROR,
                        warnings=[f"Snapshot rejected ({type(exc).__name__})."],
                    )
                    self.db.apply(key, result, attempted)
                updates = {
                    "last_outcome": result.outcome,
                    "warnings": result.warnings,
                    "metadata": result.metadata,
                }
                if result.outcome == Outcome.SUCCESS:
                    updates["last_successful_sync"] = now().isoformat()
                self.db.update_state(key, **updates)
            finally:
                self.running.discard(key)
                self.revision += 1

    async def sync_all(self):
        await asyncio.gather(
            *(
                self.sync_one(k)
                for k, c in self.connectors.items()
                if c.connection_status() == "connected"
            )
        )

    async def close(self):
        for job in list(self.jobs):
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        await asyncio.gather(*(c.close() for c in self.connectors.values()), return_exceptions=True)
