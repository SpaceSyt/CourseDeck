"""Exercise both collection entry points with real stores and synthetic source I/O."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.collection_jobs import CollectionJobs, CollectionRequest
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.library import material_sync
from coursedeck.materials import MaterialCollector
from coursedeck.sync import SyncEngine


@pytest.mark.parametrize("entry", ["automatic", "collection"])
async def test_partial_failure_and_explicit_empty_preserve_user_state(tmp_path, entry):
    db = Database(tmp_path / "tasks.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    source = "brightspace"
    course = Course(provider=source, external_id="1", name="Synthetic course")
    task = Task(
        provider=source,
        course_external_id="1",
        external_id="content-1",
        title="Assignment",
        description="Original complete instructions",
        due_at=now(),
        submission_status="submitted",
    )
    missing = task.model_copy(update={"external_id": "content-2", "title": "Not read yet"})
    db.apply(
        source,
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task, missing]),
        now().isoformat(),
    )
    db.patch_local(task.id, {"dismissed": True, "note": "Keep my reasoning"})
    original_local = next(row for row in db.tasks() if row["id"] == task.id)["local"]
    document = {
        "id": task.id,
        "provider": source,
        "course_id": course.id,
        "title": task.title,
        "body": task.description,
        "complete": True,
        "fetched_at": now().isoformat(),
        "kind": "material",
    }
    knowledge.upsert_documents([document])
    connector = SimpleNamespace(
        key=source,
        sync=AsyncMock(),
        close=AsyncMock(),
        connection_status=lambda: "connected",
    )
    engine = SyncEngine(db, [connector])
    materials = MaterialCollector(db, engine, tmp_path, knowledge_store=knowledge)
    incoming_documents = []

    async def read_materials(*args, **kwargs):
        # Only external I/O is replaced: retention, material enrichment, task writes,
        # job budgeting and both public entry points use production implementations.
        result = {"documents": [], "warnings": ["Some source pages were not read"]}
        for row in incoming_documents:
            materials.retain_document(row, result)
        return result

    materials.refresh_source = read_materials
    engine.after_sync = material_sync(db, materials, knowledge)

    async def refresh(result):
        connector.sync.return_value = result
        if entry == "automatic":
            await engine.sync_one(source, automatic=True)
        else:
            # Model an independent admission without bypassing live job deduplication.
            jobs = CollectionJobs(db, engine, materials, knowledge, enabled=True, retry_delay=0)
            try:
                queued = jobs.submit(CollectionRequest(source=source, scope="source"))
                await jobs.worker
                assert jobs.get(queued.job_id).result.outcome == result.outcome.value
            finally:
                await jobs.close()

    try:
        incoming_documents.append(document | {"body": "Truncated excerpt", "complete": False})
        partial = task.model_copy(
            update={
                "description": "",
                "due_at": None,
                "submission_status": "unknown",
                "raw_data": {"unavailable_fields": ["due_at", "submission_status"]},
            },
            deep=True,
        )
        await refresh(SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[partial]))
        current = next(row for row in db.tasks() if row["id"] == task.id)
        assert current["local"] == original_local
        assert current["description"] == task.description
        assert datetime.fromisoformat(current["due_at"]) == task.due_at
        assert current["submission_status"] == "submitted"
        assert current["field_availability"]["description"] in {"unknown", "unread"}
        retained = knowledge.get(task.id)
        assert retained["body"] == document["body"]
        assert retained["fetched_at"] == document["fetched_at"]
        assert retained["complete"] is False
        assert all(row["missing_count"] == 0 for row in db.tasks())

        await refresh(
            SyncResult(outcome=Outcome.PARSE_ERROR, warnings=["Source layout unreadable"])
        )
        assert next(row for row in db.tasks() if row["id"] == task.id)["local"] == original_local
        assert knowledge.get(task.id)["body"] == document["body"]

        # The source explicitly proves an empty description: clear only that source
        # field, retaining the independently cached material and every local decision.
        incoming_documents.clear()
        empty = task.model_copy(
            update={
                "description": "",
                "raw_data": {"field_availability": {"description": "empty"}},
            },
            deep=True,
        )
        await refresh(SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[empty]))
        current = next(row for row in db.tasks() if row["id"] == task.id)
        assert current["description"] == ""
        assert current["field_availability"]["description"] == "empty"
        assert current["local"] == original_local
        assert knowledge.get(task.id)["body"] == document["body"]
        assert len(db.tasks()) == 2
    finally:
        await engine.close()
        knowledge.close()
