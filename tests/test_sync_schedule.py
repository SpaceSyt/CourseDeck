import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coursedeck.browser import SessionPersistenceError, SessionSnapshotInvalid
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult
from coursedeck.sync import AUTO_SYNC_INTERVAL_SECONDS, SourceAccessPaused, SyncEngine


def source(key, sync):
    return SimpleNamespace(
        key=key, connection_status=lambda: "connected", sync=sync, close=AsyncMock()
    )


@pytest.mark.parametrize(
    "failure,action",
    [(SessionPersistenceError, "credential store"), (SessionSnapshotInvalid, "Reconnect")],
)
async def test_session_storage_failure_reports_action_without_reauth(tmp_path, failure, action):
    db = Database(tmp_path / "db")
    db.update_state("fixture", authorized=True)
    sync = AsyncMock(side_effect=failure("token=SECRET"))
    engine = SyncEngine(db, [source("fixture", sync)])
    try:
        await engine.sync_one("fixture")
        state = db.state("fixture")
        assert state["last_outcome"] == "error" and state["authorized"]
        assert action in state["warnings"][0]
        assert "SECRET" not in str(state)
        assert not engine.pending and not engine.running
    finally:
        await engine.close()


@pytest.mark.parametrize("operation", ["read", "restore", "capture", "save", "delete"])
async def test_session_failure_stage_survives_sync_diagnostics(tmp_path, operation):
    db = Database(tmp_path / "db")
    db.update_state("fixture", authorized=True)
    failure = SessionPersistenceError("token=SECRET", operation=operation)
    engine = SyncEngine(db, [source("fixture", AsyncMock(side_effect=failure))])
    try:
        await engine.sync_one("fixture")
        state = db.state("fixture")
        diagnostics = state["metadata"]["diagnostics"]
        assert diagnostics["stage"] == f"session_{operation}"
        assert diagnostics["category"] == f"session_{operation}_failed"
        assert state["authorized"] and state["last_outcome"] == "error"
        assert "SECRET" not in str(state)
        assert not engine.pending and not engine.running and not engine.locks["fixture"].locked()
    finally:
        await engine.close()


@pytest.mark.parametrize("source_outcome", [Outcome.SUCCESS, Outcome.PARTIAL])
async def test_material_session_failure_keeps_course_and_specific_stage(tmp_path, source_outcome):
    db = Database(tmp_path / "db")
    db.update_state("fixture", authorized=True)
    course = Course(provider="fixture", external_id="c1", name="Keep this course")
    result = SyncResult(outcome=source_outcome, courses=[course])
    engine = SyncEngine(
        db,
        [source("fixture", AsyncMock(return_value=result))],
        after_sync=AsyncMock(
            side_effect=SessionPersistenceError("token=SECRET", operation="capture")
        ),
    )
    try:
        await engine.sync_one("fixture")
        state = db.state("fixture")
        assert state["last_outcome"] == "partial" and state["authorized"]
        assert state["metadata"]["diagnostics"]["stage"] == "session_capture"
        assert state["metadata"]["diagnostics"]["category"] == "session_capture_failed"
        assert state["metadata"]["materials"]["reason"] == "session_capture_failed"
        assert any(item["id"] == course.id for item in db.source_courses())
        assert "SECRET" not in str(state)
    finally:
        await engine.close()


async def test_batch_and_manual_requests_share_one_serial_queue(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    active = 0

    async def first():
        nonlocal active
        assert active == 0
        active += 1
        calls.append("first")
        entered.set()
        await release.wait()
        active -= 1
        return SyncResult(outcome=Outcome.SUCCESS)

    async def second():
        assert active == 0
        calls.append("second")
        return SyncResult(outcome=Outcome.SUCCESS)

    engine = SyncEngine(
        Database(tmp_path / "db"), [source("first", first), source("second", second)]
    )
    batch = engine.spawn(engine.sync_all())
    await asyncio.wait_for(entered.wait(), 1)
    manual = engine.spawn(engine.sync_one("second"))
    await asyncio.sleep(0)
    await engine.sync_one("second")  # Coalesce a duplicate already waiting in the queue.
    await engine.sync_all()  # Coalesce an overlapping whole-source round.
    assert calls == ["first"]
    assert engine.pending == {"first", "second"}
    release.set()
    await asyncio.wait_for(asyncio.gather(batch, manual), 1)
    assert calls == ["first", "second"]
    assert not engine.pending and not engine.running
    await engine.close()


async def test_periodic_sync_waits_ten_minutes_and_does_not_stack(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    tick, sleeping, entered, release = (asyncio.Event() for _ in range(4))
    delays, calls = [], []

    async def sleep(seconds):
        delays.append(seconds)
        sleeping.set()
        await tick.wait()
        tick.clear()

    async def sync():
        calls.append("sync")
        entered.set()
        await release.wait()
        return SyncResult(outcome=Outcome.SUCCESS)

    connected = source("fixture", sync)
    disconnected = source("offline", AsyncMock())
    disconnected.connection_status = lambda: "not_connected"
    engine = SyncEngine(Database(tmp_path / "db"), [connected, disconnected])
    monkeypatch.setattr("coursedeck.sync.asyncio.sleep", sleep)
    job = engine.spawn(engine.poll())
    await asyncio.wait_for(sleeping.wait(), 1)
    assert AUTO_SYNC_INTERVAL_SECONDS == 600 and delays == [600] and not calls
    sleeping.clear()
    tick.set()
    await asyncio.wait_for(entered.wait(), 1)
    await real_sleep(0)
    assert delays == [600]  # The next delay starts only after this round finishes.
    release.set()
    await asyncio.wait_for(sleeping.wait(), 1)
    assert delays == [600, 600] and calls == ["sync"]
    disconnected.sync.assert_not_awaited()
    await engine.close()
    assert job.cancelled() and not engine.pending


async def test_close_cancels_running_and_queued_syncs(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        entered.set()
        await release.wait()
        return SyncResult(outcome=Outcome.SUCCESS)

    second = source("second", AsyncMock())
    engine = SyncEngine(Database(tmp_path / "db"), [source("first", first), second])
    running = engine.spawn(engine.sync_one("first"))
    await asyncio.wait_for(entered.wait(), 1)
    queued = engine.spawn(engine.sync_one("second"))
    await asyncio.sleep(0)
    await asyncio.wait_for(engine.close(), 1)
    assert running.cancelled() and queued.cancelled()
    assert not engine.running and not engine.pending and not engine.queue_lock.locked()
    assert engine.db.state("first")["metadata"]["diagnostics"]["category"] == "interrupted"
    assert engine.db.state("first")["last_outcome"] == "partial"
    second.sync.assert_not_awaited()
    second.close.assert_awaited_once()


async def test_cancelled_waiter_does_not_block_next_request(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        entered.set()
        await release.wait()
        return SyncResult(outcome=Outcome.SUCCESS)

    second = source("second", AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS)))
    engine = SyncEngine(Database(tmp_path / "db"), [source("first", first), second])
    running = engine.spawn(engine.sync_one("first"))
    await asyncio.wait_for(entered.wait(), 1)
    cancelled = engine.spawn(engine.sync_one("second"))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    replacement = engine.spawn(engine.sync_one("second"))
    release.set()
    await asyncio.wait_for(asyncio.gather(running, replacement), 1)
    second.sync.assert_awaited_once()
    await engine.close()


async def test_source_timeout_retries_but_material_failure_reports_its_own_stage(
    tmp_path, monkeypatch
):
    from coursedeck.domain import Course

    monkeypatch.setattr("coursedeck.sync.RETRY_DELAYS", ())
    db = Database(tmp_path / "db")
    timeout = source("timeout", AsyncMock(side_effect=TimeoutError))
    successful = source(
        "fixture",
        AsyncMock(
            return_value=SyncResult(
                outcome=Outcome.SUCCESS,
                courses=[Course(provider="fixture", external_id="1", name="Course")],
            )
        ),
    )
    engine = SyncEngine(db, [timeout, successful], after_sync=AsyncMock(side_effect=TimeoutError))
    await engine.sync_one("timeout")
    assert db.state("timeout")["last_outcome"] == "network_error"
    assert db.state("timeout")["metadata"]["diagnostics"]["stage"] == "source_read"
    await engine.sync_one("fixture")
    assert db.state("fixture")["last_outcome"] == "partial"
    assert db.state("fixture")["metadata"]["diagnostics"]["stage"] == "materials_read"
    assert db.state("fixture").get("last_successful_sync") is None
    await engine.close()


async def test_transient_failures_retry_serially_and_stop_at_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("coursedeck.sync.RETRY_DELAYS", (0, 0))
    sync = AsyncMock(return_value=SyncResult(outcome=Outcome.RATE_LIMITED))
    db = Database(tmp_path / "db")
    engine = SyncEngine(db, [source("fixture", sync)])
    await engine.sync_one("fixture")
    while engine.jobs:
        await asyncio.gather(*list(engine.jobs))
        await asyncio.sleep(0)
    assert sync.await_count == 3
    diagnostics = db.state("fixture")["metadata"]["diagnostics"]
    assert diagnostics["retry_attempt"] == 2 and diagnostics["next_retry_at"] is None
    assert db.state("fixture").get("last_successful_sync") is None
    assert not engine.pending and not engine.retries
    await engine.close()


async def test_recovered_network_retries_once_without_stale_success(tmp_path, monkeypatch):
    monkeypatch.setattr("coursedeck.sync.RETRY_DELAYS", (0, 0))
    sync = AsyncMock(
        side_effect=[SyncResult(outcome=Outcome.NETWORK_ERROR), SyncResult(outcome=Outcome.SUCCESS)]
    )
    db = Database(tmp_path / "db")
    engine = SyncEngine(db, [source("fixture", sync)])
    await engine.sync_one("fixture")
    assert db.state("fixture")["last_outcome"] == "network_error"
    while engine.jobs:
        await asyncio.gather(*list(engine.jobs))
        await asyncio.sleep(0)
    assert sync.await_count == 2
    assert db.state("fixture")["last_outcome"] == "success"
    assert db.state("fixture")["metadata"]["diagnostics"]["stage"] == "complete"
    await engine.close()


async def test_auth_failure_requires_manual_retry_and_exception_secrets_are_not_stored(tmp_path):
    sync = AsyncMock(
        side_effect=[SyncResult(outcome=Outcome.AUTH_REQUIRED), RuntimeError("token=secret")]
    )
    db = Database(tmp_path / "db")
    engine = SyncEngine(db, [source("fixture", sync)])
    await engine.sync_one("fixture")
    await engine.sync_all(automatic=True)
    assert sync.await_count == 1 and not engine.retries
    assert db.state("fixture")["metadata"]["diagnostics"]["action"] == "reconnect"
    await engine.sync_one("fixture")
    assert sync.await_count == 2
    assert "secret" not in str(db.state("fixture")) and "secret" not in str(db.history())
    await engine.close()


async def test_manual_sync_replaces_waiting_retry_and_maintenance_cancels_queue(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("coursedeck.sync.RETRY_DELAYS", (3600, 3600))
    sync = AsyncMock(return_value=SyncResult(outcome=Outcome.NETWORK_ERROR))
    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", sync)])
    await engine.sync_one("fixture")
    old_retry = engine.retries["fixture"]
    await engine.sync_all(automatic=True)
    assert sync.await_count == 1
    await engine.sync_one("fixture")
    assert sync.await_count == 2 and engine.retries["fixture"] is not old_retry
    async with engine.maintenance():
        assert engine.paused and engine.queue_lock.locked()
        assert engine.locks["fixture"].locked() and not engine.retries
        await engine.sync_one("fixture")
        assert sync.await_count == 2
    assert not engine.paused and not engine.queue_lock.locked()
    await engine.close()


async def test_maintenance_does_not_write_diagnostics_when_recovery_is_still_pending(tmp_path):
    db = Database(tmp_path / "db")
    state = {"diagnostics": {"stage": "source_read", "next_retry_at": "2030-01-01T00:00:00Z"}}
    db.update_state("fixture", metadata=state)
    engine = SyncEngine(db, [source("fixture", AsyncMock())])
    async with engine.maintenance(can_resume=lambda: False):
        pass
    assert db.state("fixture")["metadata"] == state
    async with engine.maintenance(can_resume=lambda: True):
        pass
    assert db.state("fixture")["metadata"]["diagnostics"]["next_retry_at"] is None
    await engine.close()


async def test_manual_request_for_later_source_is_not_repeated_in_same_batch(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        entered.set()
        await release.wait()
        return SyncResult(outcome=Outcome.SUCCESS)

    second = source("second", AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS)))
    third = source("third", AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS)))
    engine = SyncEngine(Database(tmp_path / "db"), [source("first", first), second, third])
    batch = engine.spawn(engine.sync_all())
    await asyncio.wait_for(entered.wait(), 1)
    manual = engine.spawn(engine.sync_one("third"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(batch, manual), 1)
    second.sync.assert_awaited_once()
    third.sync.assert_awaited_once()
    assert not engine.pending and not engine.running
    await engine.close()


async def test_connection_check_failure_isolated_and_next_round_recovers(tmp_path, caplog):
    from test_core import snapshot

    from coursedeck.domain import now

    db = Database(tmp_path / "db")
    result = snapshot()
    db.apply("test", result, now().isoformat())
    db.patch_local(result.tasks[0].id, {"note": "Keep this note"})
    cached = db.tasks()
    failing = source("test", AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS)))
    failing.connection_status = Mock(side_effect=[RuntimeError("token=SECRET"), "connected"])
    healthy = source("healthy", AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS)))
    engine = SyncEngine(db, [failing, healthy])
    await engine.sync_all(automatic=True)
    assert db.tasks() == cached
    failing.sync.assert_not_awaited()
    healthy.sync.assert_awaited_once()
    assert db.state("test")["last_outcome"] == "error"
    assert "SECRET" not in caplog.text + str(db.state("test"))
    await engine.sync_all(automatic=True)
    failing.sync.assert_awaited_once()
    assert healthy.sync.await_count == 2
    assert db.state("test")["last_outcome"] == "success"
    await engine.close()


async def test_failed_initial_state_write_releases_running_and_queue(tmp_path, monkeypatch):
    db = Database(tmp_path / "db")
    sync = AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS))
    engine = SyncEngine(db, [source("fixture", sync)])
    update = db.update_state
    monkeypatch.setattr(db, "update_state", Mock(side_effect=RuntimeError("Unavailable")))
    with pytest.raises(RuntimeError):
        await engine.sync_one("fixture")
    assert not engine.running and not engine.pending and not engine.queue_lock.locked()
    sync.assert_not_awaited()
    monkeypatch.setattr(db, "update_state", update)
    await engine.sync_one("fixture")
    sync.assert_awaited_once()
    await engine.close()


async def test_retry_completion_preserves_next_delayed_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("coursedeck.sync.RETRY_DELAYS", (0, 3600))
    sync = AsyncMock(return_value=SyncResult(outcome=Outcome.NETWORK_ERROR))
    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", sync)])
    await engine.sync_one("fixture")
    first_retry = engine.retries["fixture"]
    await asyncio.wait_for(first_retry, 1)
    next_retry = engine.retries["fixture"]
    assert next_retry is not first_retry and not next_retry.done()
    await engine.sync_all(automatic=True)
    assert sync.await_count == 2
    async with engine.maintenance():
        assert next_retry.cancelled() and not engine.retries
    await engine.close()


async def test_poll_survives_round_failure_and_recovers_without_stacking(
    tmp_path, monkeypatch, caplog
):
    sleeps, ticks = asyncio.Queue(), asyncio.Queue()

    async def sleep(seconds):
        await sleeps.put(seconds)
        await ticks.get()

    engine = SyncEngine(Database(tmp_path / "db"), [])
    monkeypatch.setattr("coursedeck.sync.asyncio.sleep", sleep)
    engine.sync_all = AsyncMock(side_effect=[RuntimeError("token=SECRET"), None])
    job = engine.spawn(engine.poll())
    assert await asyncio.wait_for(sleeps.get(), 1) == 600
    await ticks.put(True)
    assert await asyncio.wait_for(sleeps.get(), 1) == 600
    assert engine.sync_all.await_count == 1 and not job.done()
    await ticks.put(True)
    assert await asyncio.wait_for(sleeps.get(), 1) == 600
    assert engine.sync_all.await_count == 2 and "SECRET" not in caplog.text
    await engine.close()
    assert job.cancelled()


async def test_source_access_rechecks_maintenance_after_queue(tmp_path):
    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", AsyncMock())])
    attempted = asyncio.Event()

    async def reader():
        attempted.set()
        async with engine.source_access("fixture"):
            pytest.fail("A queued reader must not enter during maintenance")

    await engine.queue_lock.acquire()
    job = asyncio.create_task(reader())
    await attempted.wait()
    engine.paused = True
    engine.queue_lock.release()
    with pytest.raises(SourceAccessPaused):
        await job
    assert not engine.queue_lock.locked() and not engine.locks["fixture"].locked()
    await engine.close()


async def test_refresh_diagnostics_separate_queue_delay_from_execution(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("coursedeck.sync.monotonic", lambda: clock[0])

    async def read():
        clock[0] += 2.5
        return SyncResult(outcome=Outcome.SUCCESS)

    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", read)])
    await engine.queue_lock.acquire()
    job = engine.spawn(engine.sync_one("fixture"))
    await asyncio.sleep(0)
    clock[0] += 8
    engine.queue_lock.release()
    await job
    diagnostics = engine.db.state("fixture")["metadata"]["diagnostics"]
    assert diagnostics["queue_wait_seconds"] == 8
    assert diagnostics["execution_seconds"] == 2.5
    await engine.close()


@pytest.mark.parametrize("queue", ["queue_lock", "source_lock"])
async def test_collect_source_validates_after_queue_before_cancelling_retry(tmp_path, queue):
    read = AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS))
    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", read)])
    old_retry = engine.spawn(asyncio.Event().wait())
    engine.retries["fixture"] = old_retry
    connected = True

    def validate():
        if not connected:
            raise ValueError("Connection changed")

    lock = engine.queue_lock if queue == "queue_lock" else engine.locks["fixture"]
    await lock.acquire()
    job = engine.spawn(engine.collect_source("fixture", validate=validate))
    await asyncio.sleep(0)
    connected = False
    lock.release()
    with pytest.raises(ValueError, match="Connection changed"):
        await job
    read.assert_not_awaited()
    assert engine.retries["fixture"] is old_retry and not old_retry.cancelled()
    await engine.close()


async def test_queued_sync_does_not_start_after_maintenance_gate_closes(tmp_path):
    read = AsyncMock(return_value=SyncResult(outcome=Outcome.SUCCESS))
    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", read)])
    await engine.queue_lock.acquire()
    job = engine.spawn(engine.sync_one("fixture"))
    await asyncio.sleep(0)
    engine.paused = True
    engine.queue_lock.release()
    await job
    read.assert_not_awaited()
    assert not engine.pending
    await engine.close()


async def test_source_access_during_maintenance_does_not_queue(tmp_path):
    engine = SyncEngine(Database(tmp_path / "db"), [source("fixture", AsyncMock())])
    engine.paused = True
    await engine.queue_lock.acquire()
    try:
        async with asyncio.timeout(1):
            with pytest.raises(SourceAccessPaused):
                async with engine.source_access("fixture"):
                    pytest.fail("A paused source must not be read")
    finally:
        engine.queue_lock.release()
        await engine.close()
