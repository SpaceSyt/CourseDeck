import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.connectors.webassign import WebAssignConnector
from coursedeck.db import Database


class Vault:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def page(token):
    return SimpleNamespace(
        url=f"https://www.webassign.net/student?class=1&UserPass={token}",
        content=AsyncMock(return_value="<h1>My Assignments</h1>"),
    )


async def test_verified_page_updates_userpass_without_reading_another_stale_tab(tmp_path):
    vault = Vault()
    source = WebAssignConnector(Database(tmp_path / "db"), SimpleNamespace(), vault)
    current, stale = page("rotated-fixture"), page("stale-fixture")
    assert await source._save_session_page(current)
    assert "rotated-fixture" in source.session_url("https://www.webassign.net/student?class=1")
    stale.content.assert_not_awaited()
    assert "rotated-fixture" not in str(source.db.state("webassign"))


async def test_browser_owns_cookies_while_connector_only_saves_url_token(tmp_path):
    vault = Vault()
    vault.set("webassign_session", {"url": page("old").url, "cookies": [{"name": "old"}]})
    source = WebAssignConnector(Database(tmp_path / "db"), SimpleNamespace(), vault)
    current = page("rotated-fixture")
    context = SimpleNamespace(pages=[current], cookies=AsyncMock())
    assert await source.validate_session(context)
    assert vault.get("webassign_session") == {"url": current.url}
    context.cookies.assert_not_awaited()


async def test_cancelled_session_write_finishes_before_releasing_reader(tmp_path):
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    class BlockingVault(Vault):
        def set(self, key, value):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            super().set(key, value)
            finished.set()

    vault = BlockingVault()
    source = WebAssignConnector(Database(tmp_path / "db"), SimpleNamespace(), vault)
    task = asyncio.create_task(source._save_session_page(page("rotated-fixture")))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert vault.get("webassign_session")["url"] == page("rotated-fixture").url
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
