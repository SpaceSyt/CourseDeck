import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fake_connector import FakeConnector
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, Settings, SyncResult, Task, identity, now
from coursedeck.sync import SyncEngine


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.sqlite3")


def snapshot(title="Homework", complete=True, outcome=Outcome.SUCCESS):
    return SyncResult(
        outcome=outcome,
        complete=complete,
        courses=[Course(provider="test", external_id="c", name="Math")],
        tasks=[
            Task(
                provider="test",
                external_id="a",
                course_external_id="c",
                title=title,
                due_at=datetime(2026, 9, 5, 1, tzinfo=UTC),
            )
        ],
    )


def test_upsert_preserves_local_and_identity(db):
    result = snapshot()
    db.apply("test", result, now().isoformat())
    task_id = result.tasks[0].id
    original = db.tasks()[0]["first_seen_at"]
    db.patch_local(task_id, {"hidden": True, "note": "Keep this note", "priority": 3})
    result.tasks[0].title = "Changed deadline"
    result.tasks[0].submission_status = "submitted"
    db.apply("test", result, now().isoformat())
    assert len(db.tasks()) == 1
    task = db.tasks()[0]
    assert task["first_seen_at"] == original
    assert task["title"] == "Changed deadline"
    assert task["local"]["note"] == "Keep this note"
    assert task["local"]["hidden"]
    assert task["submission_status"] == "submitted"
    assert identity("a", "b:c", "d") != identity("a", "b", "c:d")
    assert identity("a", "1") != identity("b", "1")


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome.AUTH_REQUIRED,
        Outcome.NETWORK_ERROR,
        Outcome.PARSE_ERROR,
        Outcome.RATE_LIMITED,
        Outcome.ERROR,
        Outcome.PARTIAL,
    ],
)
def test_failures_and_partial_never_advance_missing(db, outcome):
    db.apply("test", snapshot(), now().isoformat())
    for _ in range(5):
        db.apply("test", SyncResult(outcome=outcome, complete=True), now().isoformat())
    task = db.tasks()[0]
    assert not task["archived"] and task["missing_count"] == 0


def test_conservative_archive_and_reappearance(db):
    db.apply("test", snapshot(), now().isoformat())
    db.patch_local("test:c:a", {"pinned": True})
    empty = SyncResult(outcome=Outcome.SUCCESS, complete=True)
    for _ in range(4):
        db.apply("test", empty, now().isoformat())
    assert not db.tasks()[0]["archived"]  # three syncs alone are insufficient
    with db.connection() as conn:
        conn.execute("UPDATE tasks SET last_seen_at=?", ((now() - timedelta(days=8)).isoformat(),))
    db.apply("test", empty, now().isoformat())
    assert db.tasks()[0]["archived"]
    db.apply("test", snapshot(), now().isoformat())
    assert not db.tasks()[0]["archived"]
    assert db.tasks()[0]["missing_count"] == 0
    assert db.tasks()[0]["local"]["pinned"]


def test_invalid_snapshot_rolls_back(db):
    result = snapshot()
    result.tasks[0].course_external_id = "missing"
    with pytest.raises(sqlite3.IntegrityError):
        db.apply("test", result, now().isoformat())
    assert db.courses() == [] and db.tasks() == []
    result = snapshot()
    result.tasks[0].provider = "other"
    with pytest.raises(ValueError):
        db.apply("test", result, now().isoformat())


def test_utc_and_dst():
    from zoneinfo import ZoneInfo

    task = snapshot().tasks[0]
    with pytest.raises(ValidationError):
        Task(**(task.model_dump() | {"due_at": datetime(2026, 1, 1)}))
    a = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    b = a.replace(fold=1)
    first = Task(**(task.model_dump() | {"due_at": a}))
    second = Task(**(task.model_dump() | {"due_at": b}))
    assert second.due_at - first.due_at == timedelta(hours=1)
    with pytest.raises(ValidationError):
        Settings(timezone="not/a/zone")


async def test_independent_sync_and_lock(db):
    mock = FakeConnector(db)
    await mock.connect()
    engine = SyncEngine(db, [mock])
    await asyncio.gather(*(engine.sync_one("fixture") for _ in range(3)))
    assert len(db.tasks()) == 6
    assert not engine.running
    await mock.disconnect()
    await engine.sync_one("fixture")
    assert len(db.tasks()) == 6
    assert db.state("fixture")["last_outcome"] == "auth_required"


def test_api_security_persistence_and_backup(tmp_path):
    app = create_app(tmp_path)
    headers = {"X-CourseDeck": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/snapshot").json()["tasks"] == []
        assert client.post("/api/sync").status_code == 403
        assert (
            client.post(
                "/api/sync", headers=headers | {"origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/snapshot", headers={"host": "evil.example"}).status_code == 400
        assert (
            client.patch(
                "/api/tasks/missing/local", headers=headers, json={"hidden": True}
            ).status_code
            == 404
        )
        app.state.db.apply("test", snapshot(), now().isoformat())
        response = client.patch(
            "/api/tasks/test:c:a/local", headers=headers, json={"note": "private"}
        )
        assert response.status_code == 200
        assert (
            client.patch(
                "/api/tasks/test:c:a/local",
                headers=headers,
                json={"submission_status": "submitted"},
            ).status_code
            == 422
        )
        assert client.post("/api/backup", headers=headers).status_code == 200
        assert "private" not in client.get("/api/debug").text
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/snapshot").json()["tasks"][0]["local"]["note"] == "private"
    restored = Database(next((tmp_path / "backups").glob("*.sqlite3")))
    assert restored.tasks()[0]["local"]["note"] == "private"
