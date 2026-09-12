from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.library import material_sync
from coursedeck.sync import SyncEngine


def assignment_result():
    return SyncResult(
        outcome=Outcome.SUCCESS,
        courses=[Course(provider="brightspace", external_id="1", name="Course")],
        tasks=[
            Task(
                provider="brightspace",
                course_external_id="1",
                external_id="dropbox-1",
                title="Assignment",
            )
        ],
    )


@pytest.mark.asyncio
async def test_material_failure_preserves_assignment_and_serial_lock(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    connector = SimpleNamespace(
        key="brightspace", sync=AsyncMock(side_effect=assignment_result), close=AsyncMock()
    )
    engine = SyncEngine(db, [connector])

    async def fail(provider, result):
        assert engine.queue_lock.locked() and engine.locks[provider].locked()
        raise RuntimeError("sensitive-token-must-not-be-logged")

    engine.after_sync = fail
    await engine.sync_one("brightspace")
    assert db.tasks()[0]["title"] == "Assignment"
    assert db.state("brightspace")["last_outcome"] == "partial"
    assert "sensitive-token" not in str(db.history())
    assert db.state("brightspace")["metadata"]["materials"]["reason"] == "read_error"
    assert not engine.running and not engine.pending


@pytest.mark.asyncio
async def test_automatic_materials_store_without_ai_and_skip_deleted_courses(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    result = assignment_result()
    result.courses.append(Course(provider="brightspace", external_id="2", name="Old course"))
    db.apply("brightspace", result, now().isoformat())
    db.patch_course("brightspace:2", {"deleted": True})
    document = {
        "id": "brightspace:1:content-3",
        "provider": "brightspace",
        "course_id": "brightspace:1",
        "title": "Syllabus",
        "body": "Instructions",
        "kind": "material",
    }
    collector = SimpleNamespace(
        refresh_source=AsyncMock(
            return_value={"documents": [document], "warnings": ["One attachment is unavailable."]}
        )
    )
    hook = material_sync(db, collector, store)
    response = await hook("brightspace", result)
    kwargs = collector.refresh_source.call_args.kwargs
    assert kwargs["course_ids"] == ["1"] and kwargs["already_locked"]
    assert [course.external_id for course in kwargs["courses"]] == ["1"]
    assert store.get(document["id"])["body"] == "Instructions"
    assert response["metadata"]["materials"]["status"] == "partial"
    assert response["warnings"]
    # The document never becomes a Todo task.
    assert len(db.tasks()) == 1


@pytest.mark.asyncio
async def test_material_body_enriches_existing_task_without_overwriting_source_facts(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    result = assignment_result()
    task = result.tasks[0]
    task.external_id = "content-3"
    task.description = "Old instructions"
    task.due_at = now()
    task.submission_status = "unknown"
    task.raw_data["unavailable_fields"] = ["description", "submission_status"]
    document = {
        "id": task.id,
        "provider": "brightspace",
        "course_id": "brightspace:1",
        "title": task.title,
        "body": "Full source instructions",
        "kind": "material",
        "complete": True,
    }
    collector = SimpleNamespace(
        refresh_source=AsyncMock(return_value={"documents": [document], "warnings": []})
    )
    due = task.due_at
    hook = material_sync(db, collector, store)
    await hook("brightspace", result)
    assert task.description == document["body"]
    assert task.due_at == due and task.submission_status == "unknown"
    assert task.raw_data["unavailable_fields"] == ["submission_status"]
    document.update(body="Only a partial excerpt", complete=False)
    task.raw_data["unavailable_fields"].append("description")
    await hook("brightspace", result)
    assert task.description == "Full source instructions"
    assert "description" in task.raw_data["unavailable_fields"]
