import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from coursedeck.collection_jobs import (
    CollectionError,
    CollectionJobs,
    CollectionRequest,
    CollectionResult,
    build_collection_router,
)
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.materials import MaterialCollector
from coursedeck.sync import SyncEngine


class Connector:
    key = "brightspace"
    display_name = "Fixture source"
    config = {}
    base_url = "https://school.example"

    def __init__(self):
        self.connected = True
        self.calls = 0
        self.outcomes = [Outcome.SUCCESS]
        self.entered = None
        self.release = None
        self.browser = SimpleNamespace(session=self.session)

    def connection_status(self):
        return "connected" if self.connected else "auth_required"

    async def sync(self):
        self.calls += 1
        assert self.engine.queue_lock.locked()
        assert self.engine.locks[self.key].locked()
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        return SyncResult(outcome=self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)])

    @asynccontextmanager
    async def session(self, timezone=None, **kwargs):
        assert self.engine.queue_lock.locked()
        assert self.engine.locks[self.key].locked()
        yield SimpleNamespace()

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "course.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    connector = Connector()
    engine = SyncEngine(db, [connector])
    connector.engine = engine
    course = Course(
        provider=connector.key,
        external_id="1",
        name="Fixture course",
        source_url="https://school.example/d2l/home/1",
    )
    task = Task(
        provider=connector.key,
        external_id="10",
        course_external_id="1",
        title="Homework",
        url="https://school.example/d2l/lms/dropbox/user/folder_submit_files.d2l?db=10&ou=1",
    )
    db.apply(
        connector.key,
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    materials = MaterialCollector(db, engine, tmp_path, knowledge_store=knowledge)
    jobs = CollectionJobs(db, engine, materials, knowledge, enabled=True, retry_delay=0)
    yield SimpleNamespace(
        db=db,
        knowledge=knowledge,
        connector=connector,
        engine=engine,
        jobs=jobs,
        materials=materials,
        task=task,
        course=course,
    )
    knowledge.close()


def source_request(**kwargs):
    return CollectionRequest(source="brightspace", scope="source", **kwargs)


async def completed(jobs, job):
    await jobs.worker
    return jobs.get(job.job_id)


def test_request_schema_rejects_unbounded_or_ambiguous_inputs():
    for value in (
        {"source": "brightspace", "scope": "source", "target_id": "1"},
        {"source": "brightspace", "scope": "task"},
        {"source": "brightspace", "scope": "source", "url": "https://evil.example"},
        {"source": "brightspace", "scope": "source", "timeout_seconds": 361},
        {"source": "brightspace", "scope": "source", "max_attempts": 3},
        {"source": "brightspace", "scope": "source", "max_attempts": True},
    ):
        with pytest.raises(ValidationError):
            CollectionRequest.model_validate(value)


async def test_disabled_no_credential_or_network_access(setup):
    setup.jobs.enabled = False
    with pytest.raises(CollectionError, match="disabled"):
        setup.jobs.submit(source_request())
    assert setup.connector.calls == 0
    assert not setup.engine.jobs


async def test_source_job_locks_success_and_status_are_read_only(setup):
    queued = setup.jobs.submit(source_request())
    assert queued.status == "queued"
    assert "brightspace" in setup.engine.pending
    result = await completed(setup.jobs, queued)
    assert result.status == "succeeded" and result.attempts == 1
    assert result.started_at and result.finished_at
    assert setup.connector.calls == 1
    for _ in range(3):
        assert setup.jobs.get(queued.job_id) == result
    assert setup.connector.calls == 1
    assert not setup.engine.pending and not setup.engine.retries
    assert setup.db.state("brightspace")["metadata"]["diagnostics"]["retry_attempt"] == 0


async def test_duplicate_request_and_idempotency_conflict(setup):
    first = setup.jobs.submit(source_request(idempotency_key="request-1"))
    same = setup.jobs.submit(source_request(idempotency_key="request-1"))
    repeated = setup.jobs.submit(source_request(idempotency_key="request-2"))
    assert first.job_id == same.job_id == repeated.job_id
    assert same.deduplicated and repeated.deduplicated
    with pytest.raises(CollectionError, match="another request"):
        setup.jobs.submit(source_request(idempotency_key="request-1", max_attempts=2))
    await completed(setup.jobs, first)
    assert setup.jobs.submit(source_request()).deduplicated
    assert setup.connector.calls == 1


async def test_transient_failure_has_bounded_retry_no_engine_retry_escape(setup):
    setup.connector.outcomes = [Outcome.NETWORK_ERROR, Outcome.SUCCESS]
    job = await completed(setup.jobs, setup.jobs.submit(source_request(max_attempts=2)))
    assert job.status == "succeeded" and job.attempts == 2
    assert setup.connector.calls == 2
    assert not setup.engine.retries
    assert setup.db.state("brightspace")["metadata"]["diagnostics"]["retry_attempt"] == 0


async def test_transient_failure_with_one_attempt_never_schedules_retry(setup):
    setup.connector.outcomes = [Outcome.NETWORK_ERROR]
    job = await completed(setup.jobs, setup.jobs.submit(source_request()))
    assert job.status == "failed" and job.attempts == 1
    diagnostics = setup.db.state("brightspace")["metadata"]["diagnostics"]
    assert diagnostics["retry_attempt"] == 0 and diagnostics["next_retry_at"] is None
    assert diagnostics["action"] == "review" and not setup.engine.retries


async def test_auth_failure_never_retries_or_attempts_new_login(setup):
    setup.connector.outcomes = [Outcome.AUTH_REQUIRED, Outcome.SUCCESS]
    job = await completed(setup.jobs, setup.jobs.submit(source_request(max_attempts=2)))
    assert job.status == "failed" and job.result.outcome == "auth_required"
    assert setup.connector.calls == 1 and job.attempts == 1
    assert not setup.engine.retries


async def test_disconnected_job_reports_auth_without_reading(setup):
    setup.connector.connected = False
    job = await completed(setup.jobs, setup.jobs.submit(source_request(max_attempts=2)))
    assert job.status == "failed" and job.result.outcome == "auth_required"
    assert setup.connector.calls == 0


async def test_course_exact_scope_and_unsupported_does_not_widen(setup, monkeypatch):
    calls = []

    async def refresh(source, courses):
        calls.append((source, [course.id for course in courses]))
        return {"documents": [], "warnings": ["A module remains unavailable"]}

    monkeypatch.setattr(setup.materials, "refresh_source", refresh)
    request = CollectionRequest(source="brightspace", scope="course", target_id=setup.course.id)
    job = await completed(setup.jobs, setup.jobs.submit(request))
    assert job.status == "partial" and not job.result.complete
    assert calls == [("brightspace", [setup.course.id])]
    assert setup.connector.calls == 0
    with pytest.raises(CollectionError, match="exact source course"):
        setup.jobs.submit(request.model_copy(update={"target_id": "workspace:1"}))
    with pytest.raises(CollectionError, match="no authorized"):
        setup.jobs.submit(source_request().model_copy(update={"source": "gmail"}))
    assert len(calls) == 1


async def test_course_auth_failure_not_retried(setup, monkeypatch):
    calls = []

    async def refresh(*args, **kwargs):
        calls.append(True)
        return {"documents": [], "errors": [{"category": "auth_required"}]}

    monkeypatch.setattr(setup.materials, "refresh_source", refresh)
    job = await completed(
        setup.jobs,
        setup.jobs.submit(
            CollectionRequest(
                source="brightspace",
                scope="course",
                target_id=setup.course.id,
                max_attempts=2,
            )
        ),
    )
    assert job.result.outcome == "auth_required" and len(calls) == 1


async def test_hourly_budget_and_queue_budget(setup):
    setup.jobs.max_per_hour = 1
    first = setup.jobs.submit(source_request())
    with pytest.raises(CollectionError, match="budget"):
        setup.jobs.submit(
            CollectionRequest(source="brightspace", scope="course", target_id=setup.course.id)
        )
    await completed(setup.jobs, first)
    assert setup.connector.calls == 1


async def test_timeout_budget_cancels_active_read_without_retry(setup, monkeypatch):
    cancelled = asyncio.Event()

    async def execute(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(setup.jobs, "_execute", execute)
    # Public validation enforces >=10s; shorten only this isolated fixture's clock budget.
    real_timeout = asyncio.timeout
    monkeypatch.setattr("coursedeck.collection_jobs.asyncio.timeout", lambda _: real_timeout(0.01))
    request = source_request(max_attempts=2)
    job = await completed(setup.jobs, setup.jobs.submit(request))
    assert job.result.outcome == "budget_exhausted" and cancelled.is_set()
    assert job.attempts == 1 and not setup.engine.pending


async def test_engine_maintenance_cancels_running_and_queued(setup):
    setup.connector.entered, setup.connector.release = asyncio.Event(), asyncio.Event()
    running = setup.jobs.submit(source_request())
    await setup.connector.entered.wait()
    queued = setup.jobs.submit(
        CollectionRequest(source="brightspace", scope="course", target_id=setup.course.id)
    )
    async with setup.engine.maintenance():
        assert setup.jobs.get(running.job_id).status == "cancelled"
        assert setup.jobs.get(queued.job_id).status == "cancelled"
        with pytest.raises(CollectionError, match="paused"):
            setup.jobs.submit(source_request())
    assert not setup.engine.pending


async def test_admission_deadline_includes_waiting_in_own_queue(setup, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("coursedeck.collection_jobs.monotonic", lambda: clock["now"])
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(request):
        calls.append(request.scope)
        started.set()
        await release.wait()
        return CollectionResult(outcome="success", message="Fixture finished")

    monkeypatch.setattr(setup.jobs, "_execute", execute)
    first = setup.jobs.submit(source_request())
    await started.wait()
    queued = setup.jobs.submit(
        CollectionRequest(
            source="brightspace",
            scope="course",
            target_id=setup.course.id,
            timeout_seconds=10,
        )
    )
    clock["now"] += 11
    release.set()
    await setup.jobs.worker
    assert setup.jobs.get(first.job_id).status == "succeeded"
    expired = setup.jobs.get(queued.job_id)
    assert expired.status == "failed" and expired.result.outcome == "budget_exhausted"
    assert expired.attempts == 0 and calls == ["source"]
    assert not setup.jobs.deadlines


async def test_cancel_worker_before_first_event_loop_turn(setup):
    job = setup.jobs.submit(source_request())
    await setup.jobs.close()
    assert setup.jobs.get(job.job_id).status == "cancelled"
    assert not setup.engine.pending and setup.connector.calls == 0


async def test_existing_normal_sync_does_not_duplicate_agent_job(setup):
    job = setup.jobs.submit(source_request())
    await setup.engine.sync_one("brightspace", automatic=True)
    await completed(setup.jobs, job)
    assert setup.connector.calls == 1
    setup.engine.pending.add("brightspace")
    with pytest.raises(CollectionError, match="already"):
        setup.jobs.submit(source_request(timeout_seconds=121))
    setup.engine.pending.clear()


async def test_results_are_bounded_and_failures_do_not_leak_exceptions(setup, monkeypatch):
    result = setup.jobs._material_result(
        {
            "documents": [{"id": str(n), "complete": True} for n in range(210)],
            "warnings": [f"warning {n}" for n in range(60)],
        }
    )
    assert result.document_count == 210 and len(result.document_ids) == 200
    assert len(result.warnings) == 50 and result.truncated

    async def execute(request):
        raise RuntimeError("https://secret.example/?token=private-password")

    monkeypatch.setattr(setup.jobs, "_execute", execute)
    job = await completed(setup.jobs, setup.jobs.submit(source_request()))
    assert job.status == "failed" and job.result.outcome == "read_error"
    assert "private-password" not in job.model_dump_json()


@pytest.mark.parametrize(
    "client,headers,allowed",
    [
        (("127.0.0.1", 2000), {"X-CourseDeck-Collection": "1"}, True),
        (("127.0.0.1", 2000), {}, False),
        (("192.0.2.4", 2000), {"X-CourseDeck-Collection": "1"}, False),
        (
            ("127.0.0.1", 2000),
            {"X-CourseDeck-Collection": "1", "Origin": "http://evil.test"},
            False,
        ),
    ],
)
async def test_http_requires_explicit_local_nonbrowser_request(setup, client, headers, allowed):
    app = FastAPI()
    app.include_router(build_collection_router(setup.jobs))
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as session:
        response = await session.post(
            "/api/collection/jobs", headers=headers, json=source_request().model_dump()
        )
        assert response.status_code == (202 if allowed else 403)
        if allowed:
            job = await completed(setup.jobs, setup.jobs.get(response.json()["job_id"]))
            result = await session.get(f"/api/collection/jobs/{job.job_id}", headers=headers)
            assert result.status_code == 200 and result.json()["status"] == "succeeded"
        else:
            assert setup.connector.calls == 0
    schema = app.openapi()
    assert schema["components"]["schemas"]["CollectionJob"]["properties"]["result"]
    assert CollectionResult.model_json_schema()["additionalProperties"] is False


async def test_task_page_read_uses_exact_id_and_persists_evidence(setup, monkeypatch):
    class Browser:
        def __init__(self, service, task_tools, expose, documents):
            assert service.scope(None) == [setup.course.id]
            assert task_tools.get(setup.task.id)["id"] == setup.task.id
            assert task_tools.get("other-task") is None
            assert documents == {}
            self.expose = expose

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def run(self, name, arguments):
            assert name == "browser_open" and arguments == {"task_id": setup.task.id}
            self.expose(
                [
                    {
                        "id": "browser:fixture",
                        "provider": "brightspace",
                        "course_id": setup.course.id,
                        "title": "Homework page",
                        "kind": "browser_page",
                        "body": "Visible instructions",
                        "url": setup.task.url,
                        "complete": False,
                        "fetched_at": now().isoformat(),
                        "checked_at": now().isoformat(),
                        "warnings": ["Only one page checked"],
                    }
                ]
            )

    monkeypatch.setattr("coursedeck.collection_jobs.BrowserTools", Browser)
    job = await completed(
        setup.jobs,
        setup.jobs.submit(
            CollectionRequest(
                source="brightspace",
                scope="task",
                target_id=setup.task.id,
            )
        ),
    )
    assert job.status == "partial" and job.result.document_ids == ["browser:fixture"]
    assert "Only one page checked" in job.result.warnings
    document = setup.knowledge.get("browser:fixture")
    assert document["source_task_id"] == setup.task.id
    assert document["body"] == "Visible instructions"
    assert setup.connector.calls == 0


async def test_document_cannot_read_mail_or_switch_credentials(setup):
    for kind, url, message in (
        ("email", "https://mail.google.com/mail/u/0/#inbox/1", "Mailbox"),
        ("material", "https://docs.google.com/document/d/1/edit", "different source"),
    ):
        setup.knowledge.upsert_documents(
            [
                {
                    "id": "document:" + kind,
                    "provider": "brightspace",
                    "course_id": setup.course.id,
                    "title": "Document",
                    "kind": kind,
                    "url": url,
                    "body": "Cached content",
                }
            ]
        )
        with pytest.raises(CollectionError, match=message):
            setup.jobs.submit(
                CollectionRequest(
                    source="brightspace",
                    scope="document",
                    target_id="document:" + kind,
                )
            )
    assert setup.connector.calls == 0 and not setup.engine.jobs


async def test_gradescope_exact_task_detail_is_serialized_and_persisted(setup, monkeypatch):
    grade = Connector()
    grade.key, grade.base_url, grade.engine = (
        "gradescope",
        "https://www.gradescope.com",
        setup.engine,
    )
    setup.engine.connectors["gradescope"] = grade
    setup.engine.locks["gradescope"] = asyncio.Lock()
    course = Course(provider="gradescope", external_id="3", name="Gradescope course")
    task = Task(
        provider="gradescope",
        external_id="4",
        course_external_id="3",
        title="Graded HW",
        graded=True,
        submission_status="graded",
        url="https://www.gradescope.com/courses/3/assignments/4/submissions/5",
        raw_data={"detail_url_observed": True},
    )
    setup.db.apply(
        "gradescope",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    stamp = now().isoformat()

    async def collect(context, selected, *, base_url):
        assert selected.id == task.id and base_url == grade.base_url
        assert setup.engine.queue_lock.locked() and setup.engine.locks["gradescope"].locked()
        return {
            "task_id": task.id,
            "outcome": "partial",
            "source_url": task.url,
            "collected_at": stamp,
            "last_confirmed_at": stamp,
            "questions": [{"label": "Q1", "score": 2, "points_possible": 3}],
            "rubric": [],
            "feedback": [],
            "submission_content": [],
            "field_availability": {},
            "warnings": ["Dynamic content not read"],
            "truncated": True,
        }

    monkeypatch.setattr("coursedeck.connectors.gradescope_detail.collect_task_detail", collect)
    job = await completed(
        setup.jobs,
        setup.jobs.submit(
            CollectionRequest(
                source="gradescope",
                scope="task",
                target_id=task.id,
            )
        ),
    )
    assert job.status == "partial" and job.result.truncated
    saved = next(item for item in setup.db.tasks() if item["id"] == task.id)
    assert saved["raw_data"]["gradescope_detail"]["questions"][0]["score"] == 2
    untouched = next(item for item in setup.db.tasks() if item["id"] == setup.task.id)
    assert "gradescope_detail" not in untouched["raw_data"]
    assert setup.connector.calls == grade.calls == 0
