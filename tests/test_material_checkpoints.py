import asyncio
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, Mock

import pytest
from test_classroom_materials import encoded, material_context
from test_materials import Response, detail, index, setup

from coursedeck.connectors.classroom_materials import collect_classroom_materials
from coursedeck.connectors.http import TransportError
from coursedeck.connectors.material_retry import retry_after_seconds, retry_material_read
from coursedeck.domain import Outcome
from coursedeck.materials import MaterialCollector


async def test_restart_resumes_identity_when_index_order_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("coursedeck.materials.MAX_BODY_READS", 2)
    calls = []
    changed = False

    async def get(url, **kwargs):
        if url.endswith("/toc"):
            body = index()
            if changed:
                body["Modules"][0]["Topics"].reverse()
            return Response(body)
        if url.endswith("/news/"):
            return Response([])
        calls.append(int(url.rsplit("/", 1)[1]))
        return Response(detail(calls[-1]))

    db, course, engine, collector = setup(tmp_path, get)
    await collector.refresh_source("brightspace", courses=[course])
    assert calls == [10, 11]
    assert collector.checkpoints.get("brightspace", course.id) == "topic:12"
    calls.clear()
    changed = True
    restarted = MaterialCollector(db, engine, tmp_path)
    await restarted.refresh_source("brightspace", courses=[course])
    assert calls[:2] == [12, 11]
    assert restarted.knowledge.get("brightspace:1:content-12")["complete"] is True
    assert not db.tasks()


async def test_cancellation_retains_document_and_unread_checkpoint(tmp_path):
    interrupted = asyncio.Event()

    async def get(url, **kwargs):
        if url.endswith("/toc"):
            return Response(index())
        if url.endswith("/11"):
            interrupted.set()
            await asyncio.Event().wait()
        return Response(detail(10))

    db, course, engine, collector = setup(tmp_path, get)
    job = asyncio.create_task(collector.refresh_source("brightspace", courses=[course]))
    await asyncio.wait_for(interrupted.wait(), 3)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    restarted = MaterialCollector(db, engine, tmp_path)
    assert restarted.checkpoints.get("brightspace", course.id) == "topic:11"
    assert restarted.knowledge.get("brightspace:1:content-10")["body"] == "Useful body 10"
    assert not engine.queue_lock.locked() and not engine.locks["brightspace"].locked()


async def test_transient_material_failure_retries_only_failed_resource(tmp_path, monkeypatch):
    monkeypatch.setattr("coursedeck.materials.MATERIAL_RETRY_DELAYS", (0, 0))
    calls = Counter()

    async def get(url, **kwargs):
        calls[url] += 1
        if url.endswith("/toc"):
            return Response(index())
        if url.endswith("/news/"):
            return Response([])
        if url.endswith("/11") and calls[url] < 3:
            return Response({}, 503)
        return Response(detail(int(url.rsplit("/", 1)[1])))

    _, course, _, collector = setup(tmp_path, get)
    result = await collector.refresh_source("brightspace", courses=[course])
    assert not result["warnings"] and not result.get("errors")
    assert next(count for url, count in calls.items() if url.endswith("/11")) == 3
    assert all(count == 1 for url, count in calls.items() if not url.endswith("/11"))
    assert all(document["complete"] for document in result["documents"])


@pytest.mark.parametrize("status,attempts", [(503, 3), (429, 3), (403, 1), (401, 1)])
async def test_terminal_failure_is_structured_and_remains_partial(
    tmp_path, monkeypatch, status, attempts
):
    monkeypatch.setattr("coursedeck.materials.MATERIAL_RETRY_DELAYS", (0, 0))
    calls = Counter()

    async def get(url, **kwargs):
        calls[url] += 1
        return Response([]) if url.endswith("/news/") else Response({}, status)

    _, course, _, collector = setup(tmp_path, get)
    result = await collector.refresh_source("brightspace", courses=[course])
    assert result["warnings"] and result["errors"]
    assert result["errors"][0]["scope"] == course.id
    assert result["errors"][0]["retry_attempt"] == attempts - 1
    assert next(count for url, count in calls.items() if url.endswith("/toc")) == attempts


async def test_restore_reads_saved_checkpoint_and_maintenance_rejects_reads(tmp_path):
    db, _, engine, collector = setup(tmp_path, None)
    collector.checkpoints.put("brightspace", "brightspace:1", "topic:12")
    backup = tmp_path / "saved.sqlite3"
    collector.knowledge.backup(backup)
    collector.checkpoints.put("brightspace", "brightspace:1", "topic:20")
    with sqlite3.connect(backup) as source, sqlite3.connect(collector.knowledge.path) as target:
        source.backup(target)
    collector.cache["stale"] = ("stamp", {})
    collector.reset_runtime_cache()
    assert not collector.cache
    assert collector.checkpoints.get("brightspace", "brightspace:1") == "topic:12"
    engine.paused = True
    with pytest.raises(RuntimeError, match="maintenance"):
        async with collector.lock("brightspace"):
            pytest.fail("Must not start a browser while restoration is active")
    assert not db.tasks()


async def test_queued_read_rechecks_maintenance_gate(tmp_path):
    _, _, engine, collector = setup(tmp_path, None)
    await engine.queue_lock.acquire()

    async def read():
        async with collector.lock("brightspace"):
            pytest.fail("Queued reader must not enter maintenance")

    job = asyncio.create_task(read())
    await asyncio.sleep(0)
    engine.paused = True
    engine.queue_lock.release()
    with pytest.raises(RuntimeError, match="maintenance"):
        await job


async def test_retry_cancellation_does_not_reenter_operation(monkeypatch):
    entered = asyncio.Event()
    operation = AsyncMock(side_effect=TransportError(Outcome.NETWORK_ERROR, "Offline"))

    async def wait(delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("coursedeck.connectors.material_retry.asyncio.sleep", wait)
    job = asyncio.create_task(
        retry_material_read(operation, errors=[], scope="course", stage="detail")
    )
    await asyncio.wait_for(entered.wait(), 3)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert operation.await_count == 1


async def test_long_retry_after_does_not_retry_earlier_than_provider_allows():
    error = TransportError(Outcome.RATE_LIMITED, "Rate limited")
    error.retry_after = 120
    operation = AsyncMock(side_effect=error)
    errors = []
    with pytest.raises(TransportError):
        await retry_material_read(operation, errors=errors, scope="course", stage="detail")
    assert operation.await_count == 1
    assert errors[0]["category"] == "rate_limited"
    assert retry_after_seconds("120") == 120
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=120))
    assert 118 <= retry_after_seconds(date) <= 120
    assert retry_after_seconds("not a date") == 0


async def test_query_does_not_reset_automatic_checkpoint(tmp_path):
    async def get(url, **kwargs):
        if url.endswith("/toc"):
            return Response(index())
        if url.endswith("/news/"):
            return Response([])
        return Response(detail(int(url.rsplit("/", 1)[1])))

    _, course, _, collector = setup(tmp_path, get)
    collector.checkpoints.put("brightspace", course.id, "topic:12")
    collector.checkpoints.put("brightspace", "courses", "brightspace:other")
    result = {"documents": [], "warnings": []}
    await collector.collect("brightspace", [course], "Notes", result)
    assert collector.checkpoints.get("brightspace", course.id) == "topic:12"
    assert collector.checkpoints.get("brightspace", "courses") == "brightspace:other"
    assert len(result["documents"]) == 4


async def test_classroom_rotates_after_budget_and_retries_transient_page(monkeypatch):
    monkeypatch.setattr("coursedeck.connectors.material_retry.RETRY_DELAYS", (0, 0))
    course, _ = material_context()
    course.source_url = f"https://classroom.google.com/c/{encoded('123')}"
    calls = Counter()

    async def read_page(page, url):
        calls[url] += 1
        if f"/m/{encoded('789')}/details" in url and calls[url] == 1:
            raise TransportError(Outcome.NETWORK_ERROR, "Network interrupted")

    connector = Mock(read_page=AsyncMock(side_effect=read_page), expand_list=AsyncMock())
    page = Mock()
    page.locator.return_value.evaluate_all = AsyncMock(
        return_value=[
            {"id": value, "kind": "Material", "title": f"Notes {value}"} for value in ("456", "789")
        ]
    )
    page.locator.return_value.wait_for = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[[], {"title": "Notes 789", "body": "Read body", "links": []}]
    )
    positions = []
    result = await collect_classroom_materials(
        connector,
        page,
        course,
        resume_identity="789",
        on_checkpoint=positions.append,
        max_body_reads=1,
    )
    assert positions == ["789", "456"]
    assert result["complete"] is False and result["warnings"]
    assert not result.get("errors")
    assert [document["complete"] for document in result["documents"]] == [False, True]
    assert next(count for url, count in calls.items() if "/m/" in url) == 2
    assert all(count == 1 for url, count in calls.items() if "/m/" not in url)
