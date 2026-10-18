"""Offline regressions for retained source evidence and cached grade projection."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coursedeck.cache_query import CacheQuery
from coursedeck.chat import ChatMessageInput, ChatService
from coursedeck.chat_tasks import TaskTools
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task
from coursedeck.knowledge import KnowledgeStore, material_freshness

OLD = datetime(2026, 9, 1, tzinfo=UTC)
NEW = datetime(2026, 10, 2, tzinfo=UTC)


@pytest.fixture
def evidence(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    course = Course(provider="brightspace", external_id="123", name="Synthetic course")
    task = Task(
        provider="brightspace",
        course_external_id="123",
        external_id="hw",
        title="Homework",
        description="OLD BODY",
        score=87,
        points_possible=100,
        graded=True,
        submission_status="submitted",
    )

    def apply(value, at, outcome=Outcome.SUCCESS):
        with patch("coursedeck.db.now", return_value=at):
            db.apply(
                "brightspace",
                SyncResult(outcome=outcome, courses=[course], tasks=[value]),
                at.isoformat(),
            )
        knowledge.index_tasks(db)
        return knowledge.get("task:" + task.id)

    yield db, knowledge, course, task, apply
    knowledge.close()


def test_unread_description_retains_old_body_and_success_time(evidence):
    db, _, _, task, apply = evidence
    before = apply(task, OLD)
    unread = task.model_copy(
        update={"description": "", "raw_data": {"unavailable_fields": ["description"]}}
    )
    after = apply(unread, NEW, Outcome.PARTIAL)
    assert before["fetched_at"] == after["fetched_at"] == OLD.isoformat()
    assert after["body"].endswith("OLD BODY")
    assert after["checked_at"] == NEW.isoformat()
    assert material_freshness(after, NEW) == "stale"
    assert after["complete"] is False
    assert db.tasks()[0]["last_seen_at"] == NEW.isoformat()
    assert db.tasks()[0]["description_fetched_at"] == OLD.isoformat()
    assert any("description" in warning for warning in after["warnings"])
    with patch("coursedeck.knowledge.now", return_value=NEW):
        cached = CacheQuery(db.path).get_task(task.id).item
    assert cached.meta.freshness == "recent"
    assert cached.data["freshness_scope"] == "task_list_observation"
    assert cached.data["description_freshness"] == "stale"
    assert cached.data["description_fetched_at"] == OLD.isoformat()
    assert cached.data["description_checked_at"] == NEW.isoformat()
    again = apply(unread, datetime(2026, 10, 3, tzinfo=UTC), Outcome.PARTIAL)
    assert again["fetched_at"] == OLD.isoformat()


def test_later_successful_body_read_refreshes_independent_timestamp(evidence):
    _, _, _, task, apply = evidence
    apply(task, OLD)
    after = apply(task.model_copy(update={"description": "NEW BODY"}), NEW)
    assert after["fetched_at"] == after["checked_at"] == NEW.isoformat()
    assert material_freshness(after, NEW) == "recent"
    assert after["body"].endswith("NEW BODY")


@pytest.mark.parametrize(
    "raw",
    [
        {"unavailable_fields": ["description"]},
        {"field_availability": {"description": "unknown"}},
    ],
)
def test_unread_first_observation_has_unknown_body_freshness(evidence, raw):
    _, _, _, task, apply = evidence
    document = apply(
        task.model_copy(update={"description": "", "raw_data": raw}), NEW, Outcome.PARTIAL
    )
    assert document["fetched_at"] is None
    assert document["checked_at"] == NEW.isoformat()
    assert material_freshness(document, NEW) == "unknown"


def test_legacy_retained_body_does_not_invent_last_success_time(evidence):
    db, knowledge, _, task, apply = evidence
    apply(task, OLD)
    with db.connection() as connection:
        row = connection.execute("SELECT payload FROM tasks WHERE id=?", (task.id,)).fetchone()
        payload = json.loads(row[0])
        payload.pop("description_fetched_at")
        payload.pop("description_checked_at")
        payload["raw_data"] = {"unavailable_fields": ["description"]}
        connection.execute(
            "UPDATE tasks SET payload=?, last_seen_at=? WHERE id=?",
            (json.dumps(payload), NEW.isoformat(), task.id),
        )
    knowledge.index_tasks(db)
    document = knowledge.get("task:" + task.id)
    assert document["body"].endswith("OLD BODY")
    assert document["fetched_at"] is None
    assert material_freshness(document, NEW) == "unknown"
    assert any("collection time is unknown" in warning for warning in document["warnings"])


def test_explicit_empty_description_is_a_successful_observation(evidence):
    _, _, _, task, apply = evidence
    apply(task, OLD)
    document = apply(
        task.model_copy(
            update={"description": "", "raw_data": {"field_availability": {"description": "empty"}}}
        ),
        NEW,
    )
    assert not document["body"].endswith("OLD BODY")
    assert document["fetched_at"] == NEW.isoformat()


@pytest.mark.parametrize("score,graded", [(87, True), (0, True), (None, False)])
async def test_get_task_and_source_evidence_preserve_grade_fields(
    evidence, tmp_path, score, graded
):
    db, knowledge, course, task, apply = evidence
    task = task.model_copy(update={"score": score, "graded": graded})
    apply(task, NEW)
    service = ChatService(db, None, tmp_path, vault=SimpleNamespace(), knowledge_store=knowledge)
    tools = TaskTools(
        service,
        [course.id],
        ChatMessageInput(message="What is my grade?", course_id=course.id),
        lambda value: value,
    )
    result = await tools.run("get_task", {"task_id": task.id})
    for projection in (
        result["task"],
        result["documents"][0]["source_fields"],
        service.task_context(task.id)["task"],
    ):
        assert projection["score"] == score
        assert projection["points_possible"] == 100
        assert projection["graded"] is graded
        assert projection["field_availability"]["score"] == (
            "known" if score is not None else "unknown"
        )
    assert result["task"]["is_completed"] is True


async def test_unavailable_grade_retains_value_but_exposes_unread_status(evidence, tmp_path):
    db, knowledge, course, task, apply = evidence
    apply(task, OLD)
    apply(
        task.model_copy(update={"score": None, "raw_data": {"unavailable_fields": ["score"]}}),
        NEW,
        Outcome.PARTIAL,
    )
    service = ChatService(db, None, tmp_path, vault=SimpleNamespace(), knowledge_store=knowledge)
    tools = TaskTools(
        service,
        [course.id],
        ChatMessageInput(message="What is my grade?", course_id=course.id),
        lambda value: value,
    )
    result = await tools.run("get_task", {"task_id": task.id})
    for projection in (result["task"], result["documents"][0]["source_fields"]):
        assert projection["score"] == 87
        assert projection["field_availability"]["score"] == "unread"
