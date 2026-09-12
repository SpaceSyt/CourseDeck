import pytest

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.task_state import DONE_STATUSES, project_task_state


def state(**values):
    return project_task_state({"submission_status": "submitted", "local": {}, **values})


@pytest.mark.parametrize("status", sorted(DONE_STATUSES))
def test_shared_completed_status_requires_current_evidence(status):
    assert state(submission_status=status)["is_completed"]
    for availability in ("missing", "unconfirmed"):
        result = state(submission_status=status, source_availability=availability)
        assert result["source_state"] == "unknown" and not result["is_completed"]
    assert not state(submission_status=status, source_status_known=False)["is_completed"]


def test_raw_unrecognized_state_is_retained_without_guessing_completion():
    task = Task(
        provider="test",
        external_id="1",
        course_external_id="c",
        title="Homework",
        submission_status="A future platform state",
    )
    result = state(**task.model_dump())
    assert result["source_status_known"] and result["source_state"] == "unknown"
    assert not result["is_completed"]
    assert task.submission_status == "A future platform state"


def test_empty_unread_and_undeclared_fields_are_distinct():
    assert state(due_at=None)["field_availability"]["due_at"] == "unknown"
    assert (
        state(due_at=None, raw_data={"field_availability": {"due_at": "empty"}})[
            "field_availability"
        ]["due_at"]
        == "empty"
    )
    assert (
        state(due_at="2026-09-10T00:00:00Z", raw_data={"unavailable_fields": ["due_at"]})[
            "field_availability"
        ]["due_at"]
        == "unread"
    )
    assert state(due_at="2026-09-10T00:00:00Z")["field_availability"]["due_at"] == "known"


def test_local_decision_overrides_derived_completion_but_not_source_evidence(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    task = Task(
        provider="test",
        external_id="1",
        course_external_id="c",
        title="Homework",
        submission_status="submitted",
    )
    db.apply(
        "test",
        SyncResult(
            outcome=Outcome.SUCCESS,
            courses=[Course(provider="test", external_id="c", name="Math")],
            tasks=[task],
        ),
        now().isoformat(),
    )
    db.patch_local(task.id, {"completion_override": "open"})
    result = db.tasks()[0]
    assert result["source_state"] == "done" and not result["is_completed"]
    db.apply("test", SyncResult(outcome=Outcome.NETWORK_ERROR), now().isoformat())
    db.patch_local(task.id, {"dismissed": True})
    result = db.tasks()[0]
    assert result["is_completed"] and not result["source_status_known"]
    assert result["submission_status"] == "submitted"


def test_explicit_unknown_preserves_previous_field_but_empty_removes_it(tmp_path):
    from datetime import UTC, datetime

    from coursedeck.changes import _state

    db = Database(tmp_path / "test.sqlite3")
    task = Task(
        provider="test",
        external_id="1",
        course_external_id="c",
        title="Homework",
        submission_status="submitted",
        due_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    result = SyncResult(
        outcome=Outcome.SUCCESS,
        courses=[Course(provider="test", external_id="c", name="Math")],
        tasks=[task],
    )
    db.apply("test", result, now().isoformat())
    task.due_at = None
    task.raw_data = {"field_availability": {"due_at": "unknown", "submission_status": "unread"}}
    db.apply("test", result, now().isoformat())
    current = db.tasks()[0]
    assert current["due_at"] and current["field_availability"]["due_at"] == "unknown"
    assert not current["is_completed"] and current["submission_status"] == "submitted"
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM tasks").fetchone()
        observed = _state(row, row["last_seen_at"])
        assert not observed["status_known"] and "due_at" in observed["unavailable_fields"]
    task.raw_data = {"field_availability": {"due_at": "empty"}}
    db.apply("test", result, now().isoformat())
    current = db.tasks()[0]
    assert current["due_at"] is None and current["field_availability"]["due_at"] == "empty"


@pytest.mark.parametrize(
    "raw",
    [
        {"unavailable_fields": "due_at"},
        {"field_availability": {"due_at": "maybe"}},
        {"field_availability": {"due_at": []}},
    ],
)
def test_connector_observation_contract_rejects_malformed_declarations(raw):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Task(provider="test", external_id="1", course_external_id="c", title="Work", raw_data=raw)
