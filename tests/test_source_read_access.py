import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.db import Database
from coursedeck.sync import SyncEngine
from coursedeck.task_links import TaskLinkReader


@pytest.mark.parametrize("change", [None, "disconnect", "maintenance"])
async def test_protected_document_waits_and_revalidates_before_browser(
    tmp_path, monkeypatch, change
):
    connected = True
    sessions = []
    context = object()

    @asynccontextmanager
    async def session(timezone):
        assert engine.queue_lock.locked()
        assert engine.locks["google_classroom"].locked()
        sessions.append(context)
        yield context

    connector = SimpleNamespace(
        key="google_classroom",
        connection_status=lambda: "connected" if connected else "not_connected",
        browser=SimpleNamespace(session=session),
        config={},
        close=AsyncMock(),
    )
    engine = SyncEngine(Database(tmp_path / "db"), [connector])
    read_body = AsyncMock(return_value="Retained fixture evidence")
    monkeypatch.setattr("coursedeck.task_links.read_google_doc_text", read_body)
    await engine.queue_lock.acquire()
    job = asyncio.create_task(
        TaskLinkReader(engine).read(
            {"id": "task", "provider": "google_classroom", "source_course_id": "course"},
            {
                "url": "https://docs.google.com/document/d/fixture/view",
                "title": "Instructions",
                "link_id": "fixture",
            },
        )
    )
    await asyncio.sleep(0)
    assert not sessions
    if change == "disconnect":
        connected = False
    if change == "maintenance":
        engine.paused = True
    engine.queue_lock.release()
    try:
        if change is None:
            result = await job
            assert result["body"] == "Retained fixture evidence"
            read_body.assert_awaited_once()
            assert sessions == [context]
        else:
            with pytest.raises(ValueError, match="changed" if change == "disconnect" else "paused"):
                await job
            assert not sessions
            read_body.assert_not_awaited()
        assert not engine.queue_lock.locked() and not engine.locks["google_classroom"].locked()
    finally:
        await engine.close()
