import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from time import monotonic

from .browser import SessionPersistenceError
from .connectors.base import Connector
from .connectors.http import TransportError
from .db import Database
from .domain import Outcome, SyncResult, now

AUTO_SYNC_INTERVAL_SECONDS = 10 * 60
RETRY_DELAYS = (15, 60)
logger = logging.getLogger(__name__)


class SourceAccessPaused(RuntimeError):
    """The source queue is unavailable while maintenance is active."""


class SyncEngine:
    def __init__(self, db: Database, connectors: list[Connector], after_sync=None):
        self.db = db
        self.after_sync = after_sync
        self.connectors = {c.key: c for c in connectors}
        self.locks = {c.key: asyncio.Lock() for c in connectors}
        self.queue_lock = asyncio.Lock()
        self.batch_lock = asyncio.Lock()
        self.pending: set[str] = set()
        self.jobs: set[asyncio.Task] = set()
        self.running: set[str] = set()
        self.revision = 0
        self.paused = False
        self.retries: dict[str, asyncio.Task] = {}

    def spawn(self, coroutine: Coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return job

    def reserve_source(self, key: str) -> bool:
        """Reserve a queued full refresh without bypassing other source readers."""
        if self.paused or key in self.pending or self.locks[key].locked():
            return False
        self.pending.add(key)
        return True

    def release_source(self, key: str):
        self.pending.discard(key)

    @asynccontextmanager
    async def source_access(self, key: str):
        """Serialize a reader with sync and recheck maintenance after queueing.

        Hold this context for the entire source browser session. Callers must
        revalidate their connection and target scope after entering it.
        """
        if self.paused:
            raise SourceAccessPaused("Source reading is paused for maintenance")
        async with self.queue_lock, self.locks[key]:
            if self.paused:
                raise SourceAccessPaused("Source reading is paused for maintenance")
            yield

    async def collect_source(self, key: str, *, validate: Callable[[], None] | None = None) -> dict:
        """Run one admitted refresh; its caller owns the deadline and retries.

        The caller reserves/releases the source for its complete job lifetime,
        including bounded retries. Validation runs after waiting for the queue.
        """
        queued_at = monotonic()
        if self.paused:
            raise SourceAccessPaused("Source reading is paused for maintenance")
        async with self.queue_lock:
            await self._sync_one(
                key,
                schedule_retry=False,
                queue_wait_seconds=monotonic() - queued_at,
                validate=validate,
                replace_retry=True,
            )
            return self.db.state(key)

    async def sync_one(self, key: str, *, automatic=False, retry_attempt=0):
        if self.paused or key in self.pending or self.locks[key].locked():
            return
        if (
            automatic
            and self.db.state(key).get("last_outcome") == Outcome.AUTH_REQUIRED
            and not getattr(self.connectors[key], "can_resume_session", lambda: False)()
        ):
            return
        if automatic and not retry_attempt and key in self.retries:
            return
        old_retry = self.retries.pop(key, None)
        if old_retry is not None and old_retry is not asyncio.current_task():
            old_retry.cancel()
        self.pending.add(key)
        queued_at = monotonic()
        try:
            # All triggers share this FIFO lock, including individual manual syncs.
            async with self.queue_lock:
                if not self.paused and not self.locks[key].locked():
                    await self._sync_one(
                        key, retry_attempt, queue_wait_seconds=monotonic() - queued_at
                    )
        finally:
            self.pending.discard(key)

    def diagnostic(self, key, stage, **values):
        metadata = self.db.state(key).get("metadata", {})
        diagnostics = metadata.get("diagnostics", {}) | {"stage": stage} | values
        self.db.update_state(key, metadata=metadata | {"diagnostics": diagnostics})
        self.revision += 1

    async def retry(self, key, delay, attempt):
        try:
            await asyncio.sleep(delay)
            await self._sync_connected(key, automatic=True, retry_attempt=attempt)
        finally:
            if self.retries.get(key) is asyncio.current_task():
                self.retries.pop(key, None)

    async def _sync_one(
        self,
        key: str,
        retry_attempt=0,
        *,
        schedule_retry=True,
        queue_wait_seconds=0.0,
        validate=None,
        replace_retry=False,
    ):
        source_queued_at = monotonic()
        async with self.locks[key]:
            if self.paused:
                raise SourceAccessPaused("Source reading is paused for maintenance")
            if validate is not None:
                validate()
            if replace_retry:
                previous_retry = self.retries.pop(key, None)
                if previous_retry is not None and previous_retry is not asyncio.current_task():
                    previous_retry.cancel()
            started_at = monotonic()
            queue_wait_seconds += started_at - source_queued_at
            self.running.add(key)
            self.revision += 1
            attempted = now().isoformat()
            stage = "source_read"
            try:
                self.db.update_state(key, last_attempted_sync=attempted)
                self.diagnostic(
                    key,
                    stage,
                    retry_attempt=retry_attempt,
                    retry_limit=len(RETRY_DELAYS),
                    next_retry_at=None,
                    queue_wait_seconds=round(queue_wait_seconds, 3),
                    execution_seconds=None,
                )
                try:
                    result = await asyncio.wait_for(self.connectors[key].sync(), timeout=180)
                except TimeoutError:
                    result = SyncResult(
                        outcome=Outcome.NETWORK_ERROR,
                        warnings=["Sync timed out. Cached data is still available."],
                    )
                except TransportError as exc:
                    result = SyncResult(outcome=exc.outcome, warnings=[exc.safe_message])
                except SessionPersistenceError as exc:
                    stage = exc.stage
                    result = SyncResult(
                        outcome=Outcome.ERROR,
                        warnings=[exc.safe_message + " Cached data is retained."],
                        metadata={"session_error": exc.diagnostics},
                    )
                except Exception:
                    # Never persist exception strings that may contain URLs, cookies or tokens.
                    result = SyncResult(
                        outcome=Outcome.ERROR,
                        warnings=["Connector failed. See setup and parser guidance."],
                    )
                failed_stage = stage
                if self.after_sync is not None and result.courses:
                    stage = "materials_read"
                    self.diagnostic(key, stage)
                    source_succeeded = result.outcome == Outcome.SUCCESS
                    try:
                        extra = await asyncio.wait_for(self.after_sync(key, result), timeout=180)
                        result.warnings.extend(extra.get("warnings", []))
                        result.metadata.update(extra.get("metadata", {}))
                    except SessionPersistenceError as exc:
                        stage = exc.stage
                        failed_stage = stage
                        result.warnings.append(exc.safe_message + " Cached materials retained.")
                        result.metadata["materials"] = {"status": "partial", "reason": exc.category}
                        result.metadata["session_error"] = exc.diagnostics
                    except TimeoutError:
                        result.warnings.append(
                            "Materials refresh timed out; cached materials retained."
                        )
                        result.metadata["materials"] = {"status": "partial", "reason": "timeout"}
                    except Exception:
                        result.warnings.append(
                            "Materials could not be refreshed; cached materials retained."
                        )
                        result.metadata["materials"] = {"status": "partial", "reason": "read_error"}
                    if result.warnings and result.outcome == Outcome.SUCCESS:
                        result.outcome = Outcome.PARTIAL
                    if source_succeeded and result.outcome != Outcome.SUCCESS:
                        failed_stage = stage
                try:
                    stage = "snapshot_apply"
                    self.diagnostic(key, stage)
                    self.db.apply(key, result, attempted)
                except Exception as exc:
                    # Rollback prevents half-applied snapshots. Store only a safe error category.
                    result = SyncResult(
                        outcome=Outcome.PARSE_ERROR,
                        warnings=[f"Snapshot rejected ({type(exc).__name__})."],
                    )
                    failed_stage = stage
                    self.db.apply(key, result, attempted)
                transient = result.outcome in {Outcome.NETWORK_ERROR, Outcome.RATE_LIMITED}
                delay = (
                    RETRY_DELAYS[retry_attempt]
                    if schedule_retry and transient and retry_attempt < len(RETRY_DELAYS)
                    else None
                )
                diagnostics = {
                    "queue_wait_seconds": round(queue_wait_seconds, 3),
                    "execution_seconds": round(monotonic() - started_at, 3),
                    "stage": "complete" if result.outcome == Outcome.SUCCESS else failed_stage,
                    "category": result.metadata.get("session_error", {}).get(
                        "category", result.outcome.value
                    ),
                    "retry_attempt": retry_attempt,
                    "retry_limit": len(RETRY_DELAYS),
                    "next_retry_at": (now() + timedelta(seconds=delay)).isoformat()
                    if delay is not None
                    else None,
                    "action": "reconnect"
                    if result.outcome == Outcome.AUTH_REQUIRED
                    else "retry_scheduled"
                    if delay is not None
                    else "review"
                    if result.outcome != Outcome.SUCCESS
                    else "none",
                }
                result.metadata["diagnostics"] = diagnostics
                updates = {
                    "last_outcome": result.outcome,
                    "warnings": result.warnings,
                    "metadata": result.metadata,
                }
                if result.outcome == Outcome.SUCCESS:
                    updates["last_successful_sync"] = now().isoformat()
                self.db.update_state(key, **updates)
                if delay is not None and not self.paused:
                    self.retries[key] = self.spawn(self.retry(key, delay, retry_attempt + 1))
            except asyncio.CancelledError:
                self.db.update_state(
                    key,
                    last_outcome=Outcome.PARTIAL,
                    warnings=["Sync interrupted; cached data retained."],
                )
                self.diagnostic(
                    key,
                    stage,
                    category="interrupted",
                    action="review",
                    next_retry_at=None,
                    execution_seconds=round(monotonic() - started_at, 3),
                )
                raise
            finally:
                self.running.discard(key)
                self.revision += 1

    async def _sync_connected(self, key, *, automatic=False, retry_attempt=0):
        try:
            if (
                self.connectors[key].connection_status() == "connected"
                or getattr(self.connectors[key], "can_resume_session", lambda: False)()
            ):
                await self.sync_one(key, automatic=automatic, retry_attempt=retry_attempt)
        except Exception as exc:
            # Exception text can contain credentials; retain only the error category.
            logger.error("Source scheduling failed for %s (%s)", key, type(exc).__name__)
            try:
                self.db.update_state(
                    key,
                    last_outcome=Outcome.ERROR,
                    warnings=["Sync could not run; cached data retained."],
                )
                self.diagnostic(
                    key, "scheduler", category="error", action="review", next_retry_at=None
                )
            except Exception as state_error:
                logger.error("Sync status could not be saved (%s)", type(state_error).__name__)

    async def sync_all(self, *, automatic=False):
        if self.paused or self.batch_lock.locked():
            return
        async with self.batch_lock:
            # Reserve the whole round in the shared serial queue so a manual request
            # cannot complete a later source and then repeat it within this same round.
            await asyncio.gather(
                *(
                    self.spawn(self._sync_connected(key, automatic=automatic))
                    for key in self.connectors
                )
            )

    async def poll(self):
        while True:
            await asyncio.sleep(AUTO_SYNC_INTERVAL_SECONDS)
            # Await the round so a slow sync never builds up periodic jobs.
            try:
                await self.sync_all(automatic=True)
            except Exception as exc:
                logger.error("Automatic sync round failed (%s)", type(exc).__name__)

    @asynccontextmanager
    async def maintenance(self, *, can_resume=lambda: True):
        """Caller must also stop Gmail/Chat and block HTTP mutations before entering."""
        if self.paused:
            raise RuntimeError("Maintenance is already active")
        self.paused = True
        try:
            jobs = [job for job in self.jobs if job is not asyncio.current_task()]
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self.retries.clear()
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self.queue_lock)
                for lock in self.locks.values():
                    await stack.enter_async_context(lock)
                await asyncio.gather(*(c.close() for c in self.connectors.values()))
                yield
        finally:
            if can_resume():
                for key in self.connectors:
                    metadata = self.db.state(key).get("metadata", {})
                    diagnostic = metadata.get("diagnostics", {})
                    if diagnostic.get("next_retry_at"):
                        self.diagnostic(
                            key,
                            diagnostic.get("stage", "source_read"),
                            next_retry_at=None,
                            action="review",
                        )
            self.paused = False
            self.revision += 1

    async def close(self):
        for job in list(self.jobs):
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        await asyncio.gather(*(c.close() for c in self.connectors.values()), return_exceptions=True)
