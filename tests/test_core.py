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


def test_missing_tasks_remain_visible_and_reappearance_clears_warning(db):
    db.apply("test", snapshot(), now().isoformat())
    db.patch_local("test:c:a", {"pinned": True})
    empty = SyncResult(outcome=Outcome.SUCCESS, complete=True)
    for _ in range(4):
        db.apply("test", empty, now().isoformat())
    assert not db.tasks()[0]["archived"]
    assert db.tasks()[0]["source_availability"] == "missing"
    with db.connection() as conn:
        conn.execute("UPDATE tasks SET last_seen_at=?", ((now() - timedelta(days=8)).isoformat(),))
    db.apply("test", empty, now().isoformat())
    assert not db.tasks()[0]["archived"]  # Disappearance never hides or completes a task.
    assert db.tasks()[0]["submission_status"] == "unknown"
    db.apply("test", snapshot(), now().isoformat())
    assert not db.tasks()[0]["archived"]
    assert db.tasks()[0]["missing_count"] == 0
    assert db.tasks()[0]["source_availability"] == "present"
    assert db.tasks()[0]["local"]["pinned"]


def test_partial_list_coverage_is_limited_to_course_and_task_type(db):
    result = snapshot()
    result.courses.append(Course(provider="test", external_id="other", name="Other"))
    result.tasks = [
        result.tasks[0].model_copy(update={"external_id": "assignment:1"}),
        result.tasks[0].model_copy(update={"external_id": "assignment:2"}),
        result.tasks[0].model_copy(update={"external_id": "content:1"}),
        result.tasks[0].model_copy(
            update={"external_id": "assignment:1", "course_external_id": "other"}
        ),
    ]
    db.apply("test", result, now().isoformat())
    partial = SyncResult(
        outcome=Outcome.PARTIAL,
        courses=result.courses[:1],
        tasks=result.tasks[:1],
        covered_task_scopes=[
            {"course_external_id": "c", "external_id_prefix": "assignment:"},
            {"course_external_id": "c", "external_id_prefix": "assignment:2"},
        ],
    )
    db.apply("test", partial, now().isoformat())
    tasks = {task["id"]: task for task in db.tasks()}
    assert tasks[identity("test", "c", "assignment:2")]["missing_count"] == 1
    for key in [
        identity("test", "c", "content:1"),
        identity("test", "other", "assignment:1"),
    ]:
        assert tasks[key]["source_availability"] == "unconfirmed"
        assert tasks[key]["missing_count"] == 0
    assert tasks[identity("test", "c", "assignment:1")]["source_availability"] == "present"
    # An omitted/incomplete scope in the next attempt cannot compound disappearance.
    db.apply("test", SyncResult(outcome=Outcome.PARTIAL), now().isoformat())
    assert next(t for t in db.tasks() if t["missing_count"])["missing_count"] == 1


def test_whole_course_coverage_and_fatal_failure(db):
    result = snapshot()
    db.apply("test", result, now().isoformat())
    covered = SyncResult(
        outcome=Outcome.NETWORK_ERROR,
        courses=result.courses,
        covered_task_scopes=[{"course_external_id": "c"}],
    )
    db.apply("test", covered, now().isoformat())
    assert db.tasks()[0]["source_availability"] == "unconfirmed"
    assert db.tasks()[0]["missing_count"] == 0
    covered.outcome = Outcome.PARTIAL
    db.apply("test", covered, now().isoformat())
    assert db.tasks()[0]["source_availability"] == "missing"


def test_unread_course_cannot_be_declared_covered(db):
    db.apply("test", snapshot(), now().isoformat())
    invalid = SyncResult(
        outcome=Outcome.PARTIAL,
        covered_task_scopes=[{"course_external_id": "c"}],
    )
    with pytest.raises(ValueError, match="unread course"):
        db.apply("test", invalid, now().isoformat())
    assert db.tasks()[0]["source_availability"] == "present"


def test_unread_submission_preserves_evidence_but_marks_status_unknown(db):
    result = snapshot()
    result.tasks[0].submission_status = "submitted"
    db.apply("test", result, now().isoformat())
    assert db.tasks()[0]["source_status_known"]
    result.tasks[0].submission_status = "unknown"
    result.tasks[0].raw_data = {"unavailable_fields": ["submission_status"]}
    db.apply("test", result, now().isoformat())
    task = db.tasks()[0]
    assert task["submission_status"] == "submitted"
    assert not task["source_status_known"]
    result.tasks[0].submission_status = "new"
    result.tasks[0].raw_data = {}
    db.apply("test", result, now().isoformat())
    task = db.tasks()[0]
    assert task["submission_status"] == "new" and task["source_status_known"]


@pytest.mark.parametrize("outcome", [Outcome.PARTIAL, Outcome.NETWORK_ERROR])
def test_unread_cached_completion_is_unconfirmed_without_deleting_evidence(db, outcome):
    result = snapshot()
    result.tasks[0].submission_status = "submitted"
    db.apply("test", result, now().isoformat())
    db.apply("test", SyncResult(outcome=outcome), now().isoformat())
    task = db.tasks()[0]
    assert task["submission_status"] == "submitted"
    assert task["source_availability"] == "unconfirmed"
    assert not task["source_status_known"] and task["missing_count"] == 0
    with db.connection() as connection:
        connection.execute("DELETE FROM sync_history")
    assert Database(db.path).tasks()[0]["source_availability"] == "unconfirmed"
    db.apply("test", result, now().isoformat())
    task = db.tasks()[0]
    assert task["source_status_known"] and task["source_availability"] == "present"


def test_v6_migration_restores_automatically_archived_tasks(db):
    db.apply("test", snapshot(), now().isoformat())
    db.patch_local("test:c:a", {"note": "Retain evidence"})
    with db.connection() as connection:
        connection.execute("UPDATE tasks SET archived=1, missing_count=4")
        connection.execute("PRAGMA user_version=6")
    migrated = Database(db.path)
    task = migrated.tasks()[0]
    assert not task["archived"]
    assert task["source_availability"] == "missing"
    assert task["local"]["note"] == "Retain evidence"


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
        backup_response = client.post("/api/backup", headers=headers)
        assert backup_response.status_code == 200
        backup_id = backup_response.json()["backup"]["id"]
        assert client.post(
            "/api/recovery/preflight", headers=headers, json={"id": backup_id}
        ).json()["valid"]
        assert "private" not in client.get("/api/debug").text
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/snapshot").json()["tasks"][0]["local"]["note"] == "private"
    restored = Database(tmp_path / "backups" / backup_id / "coursedeck.sqlite3")
    assert restored.tasks()[0]["local"]["note"] == "private"
