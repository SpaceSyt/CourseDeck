import asyncio
import json
from unittest.mock import Mock

from fake_connector import FakeConnector
from test_core import snapshot

from coursedeck.connectors.classroom import ClassroomConnector
from coursedeck.db import Database
from coursedeck.domain import Outcome, now
from coursedeck.sync import SyncEngine


def test_parse_failure_does_not_erase_previously_known_deadline(tmp_path):
    db = Database(tmp_path / "db")
    result = snapshot()
    db.apply("test", result, now().isoformat())
    known = db.tasks()[0]["due_at"]
    result.outcome = Outcome.PARTIAL
    result.tasks[0].due_at = None
    result.tasks[0].raw_data = {"unavailable_fields": ["due_at"]}
    db.apply("test", result, now().isoformat())
    assert db.tasks()[0]["due_at"] == known
    result.tasks[0].raw_data = {}
    result.outcome = Outcome.SUCCESS
    db.apply("test", result, now().isoformat())
    assert db.tasks()[0]["due_at"] is None  # a positively confirmed removed due date is different


async def test_slow_or_broken_connector_cannot_block_other_provider(tmp_path):
    db = Database(tmp_path / "db")
    fast = FakeConnector(db)
    await fast.connect()
    broken = FakeConnector(db)
    broken.key = "broken"
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail():
        entered.set()
        await release.wait()
        raise ValueError("token=NEVER_LOG_THIS")

    broken.sync = fail
    engine = SyncEngine(db, [fast, broken])
    job = asyncio.create_task(engine.sync_one("broken"))
    await entered.wait()
    await engine.sync_one("fixture")
    assert len(db.tasks()) == 6
    assert "broken" in engine.running
    release.set()
    await job
    assert db.state("broken")["last_outcome"] == "error"
    assert "NEVER_LOG_THIS" not in json.dumps(db.history())


async def test_google_refresh_persists_to_vault_not_database(tmp_path, monkeypatch):
    db = Database(tmp_path / "db")
    vault = Mock()
    vault.get.return_value = {"synthetic": True}
    credentials = Mock(valid=False, token="secret-access-token")
    credentials.to_json.return_value = '{"refresh_token":"secret-refresh-token"}'
    monkeypatch.setattr(
        "coursedeck.connectors.classroom.Credentials.from_authorized_user_info",
        lambda raw, scopes: credentials,
    )
    from coursedeck.domain import SyncResult

    async def sync(self):
        return SyncResult(outcome=Outcome.SUCCESS, complete=True)

    monkeypatch.setattr("coursedeck.connectors.classroom.ClassroomTransport.sync", sync)
    connector = ClassroomConnector(db, vault)
    await connector.sync()
    credentials.refresh.assert_called_once()
    vault.set.assert_called_once_with("google_classroom", {"refresh_token": "secret-refresh-token"})
    assert "secret" not in json.dumps(db.state("google_classroom"))
