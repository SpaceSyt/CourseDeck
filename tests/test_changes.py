import sqlite3
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coursedeck.changes import build_changes_router, list_changes, summary
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now


def snapshot(task_id="assignment", course_id="math", **changes):
    course = Course(provider="test", external_id=course_id, name=course_id)
    task = Task(
        provider="test",
        external_id=task_id,
        course_external_id=course_id,
        title="Homework",
        description="Original instructions",
        submission_status="new",
        due_at=now() + timedelta(days=7),
        url="https://school.example/task?id=1&token=secret",
    )
    for key, value in changes.items():
        setattr(task, key, value)
    return SyncResult(outcome=Outcome.SUCCESS, complete=True, courses=[course], tasks=[task])


def apply(db, result):
    db.apply("test", result, now().isoformat())


def kinds(db):
    return [change["kind"] for change in list_changes(db)["changes"]]


def test_upgrade_seeds_cache_without_reporting_existing_tasks_as_new(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot()
    apply(db, result)
    db.patch_local(result.tasks[0].id, {"note": "Keep this", "dismissed": True})
    # Simulate a database from before change tracking, without altering its task payloads.
    with db.connection() as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("DROP TABLE task_changes")
        connection.execute("DROP TABLE task_change_baselines")
        connection.execute("DROP TABLE change_settings")
    upgraded = Database(db.path)
    assert summary(upgraded) == {"unread_count": 0, "critical_unread_count": 0}
    apply(upgraded, result)
    assert list_changes(upgraded)["total"] == 0
    assert upgraded.tasks()[0]["local"]["note"] == "Keep this"
    assert upgraded.tasks()[0]["local"]["dismissed"]
    added = result.model_copy(deep=True)
    added.tasks.append(added.tasks[0].model_copy(update={"external_id": "new-assignment"}))
    apply(upgraded, added)
    assert kinds(upgraded) == ["added"]
    assert list_changes(Database(db.path))["total"] == 1


def test_deadline_status_and_body_changes_preserve_evidence_and_deduplicate(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot()
    apply(db, result)
    old_deadline = result.tasks[0].model_dump(mode="json")["due_at"]
    result.tasks[0].due_at -= timedelta(days=2)
    result.tasks[0].description = "Read the revised instructions carefully."
    result.tasks[0].submission_status = "submitted"
    apply(db, result)
    changes = list_changes(db)["changes"]
    assert set(kinds(db)) == {"added", "deadline_earlier", "body_changed", "status_changed"}
    deadline = next(change for change in changes if change["kind"] == "deadline_earlier")
    assert deadline["before"] == old_deadline and deadline["severity"] == "critical"
    assert deadline["after"] == result.tasks[0].model_dump(mode="json")["due_at"]
    assert deadline["evidence"]["url"] == "https://school.example/task?id=1"
    assert deadline["task_id"] == result.tasks[0].id and deadline["source_course_id"] == "test:math"
    count = len(changes)
    apply(db, result)
    assert list_changes(db)["total"] == count
    result.tasks[0].due_at += timedelta(days=3)
    apply(db, result)
    assert kinds(db)[0] == "deadline_delayed"


def test_missing_and_failed_refresh_are_distinct_and_never_create_source_edits(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot(submission_status="submitted")
    apply(db, result)
    failure = SyncResult(outcome=Outcome.NETWORK_ERROR)
    apply(db, failure)
    assert kinds(db) == ["unrefreshed", "added"]
    assert list_changes(db)["changes"][0]["severity"] == "info"
    apply(db, failure)
    assert kinds(db) == ["unrefreshed", "added"]
    missing = SyncResult(
        outcome=Outcome.PARTIAL,
        courses=result.courses,
        covered_task_scopes=[{"course_external_id": "math"}],
    )
    apply(db, missing)
    assert kinds(db)[0] == "missing"
    missing_count = list_changes(db)["total"]
    apply(db, missing)
    assert list_changes(db)["total"] == missing_count
    assert db.tasks()[0]["submission_status"] == "submitted"
    apply(db, result)
    assert "restored" in kinds(db)
    assert not {"reopened", "status_changed", "deadline_changed", "body_changed"} & set(kinds(db))


def test_unavailable_fields_retain_known_values_and_reopening_uses_confirmed_evidence(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot(submission_status="graded")
    apply(db, result)
    original = db.tasks()[0]
    result.tasks[0].submission_status = "unknown"
    result.tasks[0].description = ""
    result.tasks[0].due_at = None
    result.tasks[0].raw_data = {
        "unavailable_fields": ["description", "due_at", "submission_status"]
    }
    apply(db, result)
    assert kinds(db) == ["fields_unavailable", "added"]
    assert db.tasks()[0]["description"] == original["description"]
    assert db.tasks()[0]["due_at"] == original["due_at"]
    result.tasks[0].raw_data = {"unavailable_fields": ["description", "due_at"]}
    result.tasks[0].submission_status = "reopened"
    apply(db, result)
    reopened = next(
        change for change in list_changes(db)["changes"] if change["kind"] == "reopened"
    )
    assert reopened["before"] == "graded" and reopened["after"] == "reopened"
    assert reopened["severity"] == "critical"
    assert "body_changed" not in kinds(db) and "deadline_changed" not in kinds(db)


def test_snapshot_rejection_rolls_back_events_and_baseline(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot()
    apply(db, result)
    old = list_changes(db)
    result.tasks[0].course_external_id = "nonexistent"
    with pytest.raises(sqlite3.IntegrityError):
        apply(db, result)
    assert list_changes(db) == old
    assert len(db.tasks()) == 1


def test_change_hook_failure_rolls_back_task_history_and_events_together(tmp_path, monkeypatch):
    import coursedeck.changes as tracking

    db = Database(tmp_path / "db.sqlite3")
    result = snapshot()
    apply(db, result)
    old_task, old_changes, old_history = db.tasks()[0], list_changes(db), db.history()
    original_hook = tracking.record_sync

    def fail_after_recording(*args):
        original_hook(*args)
        raise RuntimeError("Simulated transaction failure")

    monkeypatch.setattr(tracking, "record_sync", fail_after_recording)
    result.tasks[0].title = "Changed title"
    with pytest.raises(RuntimeError, match="transaction failure"):
        apply(db, result)
    assert db.tasks()[0] == old_task
    assert list_changes(db) == old_changes and db.history() == old_history
    monkeypatch.setattr(tracking, "record_sync", original_hook)
    apply(db, result)
    assert kinds(db).count("title_changed") == 1


def test_identical_sync_never_resurrects_read_notifications(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot()
    apply(db, result)
    app = FastAPI()
    app.include_router(build_changes_router(db))
    client = TestClient(app)
    client.post("/api/changes/read", json={})
    for _ in range(3):
        apply(db, result)
    assert summary(db) == {"unread_count": 0, "critical_unread_count": 0}
    assert list_changes(db)["total"] == 1


def test_read_filter_pagination_course_merge_and_disabled_courses(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    first = snapshot()
    second = snapshot("essay", "writing")
    first.courses.extend(second.courses)
    first.tasks.extend(second.tasks)
    apply(db, first)
    app = FastAPI()
    app.include_router(build_changes_router(db))
    client = TestClient(app)
    page = client.get("/api/changes", params={"limit": 1}).json()
    assert page["total"] == page["unread_count"] == 2
    assert len(page["changes"]) == 1
    key = page["changes"][0]["id"]
    assert client.patch(f"/api/changes/{key}", json={"read": True}).json()["unread_count"] == 1
    assert client.get("/api/changes", params={"unread": True}).json()["total"] == 1
    assert client.patch(f"/api/changes/{key}", json={"read": False}).json()["unread_count"] == 2
    db.merge_courses("test:math", ["test:writing"])
    merged = client.get("/api/changes", params={"course_id": "test:writing"}).json()
    assert merged["total"] == 2 and {change["course_id"] for change in merged["changes"]} == {
        "test:math"
    }
    assert len({change["source_course_id"] for change in merged["changes"]}) == 2
    assert client.post("/api/changes/read", json={"ids": [key]}).json()["unread_count"] == 1
    assert (
        client.get("/api/changes", params={"kind": "added", "critical": True}).json()["total"] == 2
    )
    assert client.get("/api/changes", params={"kind": "unknown"}).status_code == 400
    assert client.patch("/api/changes/99999", json={"read": True}).status_code == 404
    db.patch_course("test:math", {"disabled": True})
    assert client.get("/api/changes").json()["total"] == 0
    assert summary(db) == {"unread_count": 0, "critical_unread_count": 0}
    db.patch_course("test:math", {"disabled": False})
    assert list_changes(db)["total"] == 2
    assert client.post("/api/changes/read", json={}).json()["unread_count"] == 0


def test_only_line_endings_are_ignored_but_code_indentation_is_preserved(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot(description="Line one\n  indented code")
    apply(db, result)
    result.tasks[0].description = "Line one\r\n  indented code"
    apply(db, result)
    assert kinds(db) == ["added"]
    result.tasks[0].description = "Line one\n    indented code"
    apply(db, result)
    assert kinds(db)[0] == "body_changed"


@pytest.mark.parametrize("previous", ["submitted", "graded", "returned", "completed"])
@pytest.mark.parametrize("current", ["open", "assigned", "missing", "incomplete"])
def test_all_confirmed_done_to_unfinished_states_are_reopened(tmp_path, previous, current):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot(submission_status=previous)
    apply(db, result)
    result.tasks[0].submission_status = current
    apply(db, result)
    change = list_changes(db)["changes"][0]
    assert change["kind"] == "reopened" and change["severity"] == "critical"
    assert change["before"] == previous and change["after"] == current
    # Explicit submission-status 'missing' is distinct from absence of the source task.
    assert db.tasks()[0]["source_availability"] == "present"
    apply(db, result)
    assert kinds(db).count("reopened") == 1


def test_returned_matches_frontend_done_and_unknown_does_not_prove_reopening(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot(submission_status="submitted")
    apply(db, result)
    result.tasks[0].submission_status = "returned"
    apply(db, result)
    assert kinds(db)[0] == "status_changed" and "reopened" not in kinds(db)
    result.tasks[0].submission_status = "unknown"
    apply(db, result)
    assert kinds(db)[0] == "fields_unavailable" and "reopened" not in kinds(db)


def test_searches_title_and_before_after_body_with_literal_patterns_and_unicode(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    result = snapshot(title="Algebra homework", description="旧正文 100% credit")
    apply(db, result)
    result.tasks[0].description = "修订正文 keep_indentation"
    apply(db, result)
    app = FastAPI()
    app.include_router(build_changes_router(db))
    client = TestClient(app)
    for query in ["旧正文", "修订正文", "100%", "keep_indentation"]:
        page = client.get("/api/changes", params={"query": query}).json()
        assert page["total"] == 1 and page["changes"][0]["kind"] == "body_changed"
    assert client.get("/api/changes", params={"query": "Algebra"}).json()["total"] == 2
    assert client.get("/api/changes", params={"query": "100_"}).json()["total"] == 0
    assert (
        client.get("/api/changes", params={"query": "Algebra", "kind": "added"}).json()["total"]
        == 1
    )
