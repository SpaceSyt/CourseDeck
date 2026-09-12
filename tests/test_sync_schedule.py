import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.db import Database
from coursedeck.domain import Outcome, SyncResult
from coursedeck.sync import AUTO_SYNC_INTERVAL_SECONDS, SyncEngine


def source(key, sync):
    return SimpleNamespace(
        key=key, connection_status=lambda: "connected", sync=sync, close=AsyncMock()
    )


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


async def test_periodic_sync_waits_thirty_minutes_and_does_not_stack(tmp_path, monkeypatch):
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
    assert AUTO_SYNC_INTERVAL_SECONDS == 1800 and delays == [1800] and not calls
    sleeping.clear()
    tick.set()
    await asyncio.wait_for(entered.wait(), 1)
    await real_sleep(0)
    assert delays == [1800]  # The next delay starts only after this round finishes.
    release.set()
    await asyncio.wait_for(sleeping.wait(), 1)
    assert delays == [1800, 1800] and calls == ["sync"]
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
