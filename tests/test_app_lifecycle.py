import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from coursedeck import app as app_module
from coursedeck.domain import Settings
from coursedeck.revisions import DatabaseRevision


@pytest.mark.parametrize("engine_failure", [False, True])
async def test_mail_shutdown_failure_still_closes_engine_and_readers(
    tmp_path, monkeypatch, engine_failure
):
    revisions = []

    def revision_reader(path):
        reader = DatabaseRevision(path)
        reader.close = Mock(wraps=reader.close)
        revisions.append(reader)
        return reader

    monkeypatch.setattr(app_module, "DatabaseRevision", revision_reader)
    app = app_module.create_app(tmp_path)
    app.state.db.save_settings(Settings(startup_sync=False))

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.gmail, "poll", idle)
    monkeypatch.setattr(app.state.engine, "poll", idle)
    monkeypatch.setattr(
        app.state.gmail, "close", AsyncMock(side_effect=RuntimeError("mail close failed"))
    )
    original_engine_close = app.state.engine.close

    async def close_engine():
        await original_engine_close()
        if engine_failure:
            raise RuntimeError("engine close failed")

    engine_close = AsyncMock(side_effect=close_engine)
    monkeypatch.setattr(app.state.engine, "close", engine_close)
    readers = []
    for reader in (app.state.associations, app.state.knowledge):
        close = Mock(wraps=reader.close)
        monkeypatch.setattr(reader, "close", close)
        readers.append(close)
    expected = "engine close failed" if engine_failure else "mail close failed"
    with pytest.raises(RuntimeError, match=expected):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
    engine_close.assert_awaited_once()
    for reader in readers:
        reader.assert_called_once()
    assert len(revisions) == 1
    revisions[0].close.assert_called_once()
    assert not app.state.engine.jobs


async def test_startup_failure_cleans_up_started_background_tasks(tmp_path, monkeypatch):
    app = app_module.create_app(tmp_path)

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.gmail, "poll", idle)
    monkeypatch.setattr(app.state.engine, "poll", idle)
    monkeypatch.setattr(
        app.state.db, "settings", Mock(side_effect=RuntimeError("settings unavailable"))
    )
    mail_close = AsyncMock(wraps=app.state.gmail.close)
    engine_close = AsyncMock(wraps=app.state.engine.close)
    readers_close = Mock(wraps=app.state.knowledge.close)
    monkeypatch.setattr(app.state.gmail, "close", mail_close)
    monkeypatch.setattr(app.state.engine, "close", engine_close)
    monkeypatch.setattr(app.state.knowledge, "close", readers_close)
    try:
        with pytest.raises(RuntimeError, match="settings unavailable"):
            async with app.router.lifespan_context(app):
                pytest.fail("Startup should have failed")
        mail_close.assert_awaited_once()
        engine_close.assert_awaited_once()
        readers_close.assert_called_once()
        assert not app.state.engine.jobs
    finally:
        # Keep this regression test isolated even when testing a broken startup implementation.
        await app.state.engine.close()
        app.state.close_readers()
