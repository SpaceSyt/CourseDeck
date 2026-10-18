"""Explicit, bounded collection in the existing application's browser owner.

No worker or credentials are created by the MCP process. Jobs are process-local,
share SyncEngine's queue/locks, and are cancelled by engine maintenance/shutdown.
"""

import asyncio
import hashlib
import ipaddress
from collections import deque
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .chat_browser import BrowserReadContext, BrowserReadScope, BrowserTools
from .chat_browser_policy import PROVIDERS
from .connectors.capabilities import connector_capabilities
from .domain import Course, now
from .knowledge import redact_source_text, safe_source_url
from .sync import SourceAccessPaused
from .task_links import TaskLinkReader

ProjectedText = Annotated[str, AfterValidator(redact_source_text)]


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_]+$",
        description="An existing connected course source. Remote Gmail reading is unsupported.",
    )
    scope: Literal["source", "course", "task", "document"] = Field(
        description="source refreshes one whole source; course reads materials for one exact "
        "source course; task/document reads one cached target. Narrow scope never expands.",
    )
    target_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Cached task/document ID or exact source course ID; omitted for source scope.",
    )
    timeout_seconds: int = Field(default=120, ge=10, le=360, strict=True)
    max_attempts: int = Field(default=1, ge=1, le=2, strict=True)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def exact_scope(self):
        if (self.scope == "source") != (self.target_id is None):
            raise ValueError("source scope has no target_id; all narrower scopes require one")
        return self


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str
    message: ProjectedText
    source_url: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    document_count: int = 0
    truncated: bool = False
    complete: bool = False
    warnings: list[ProjectedText] = Field(default_factory=list)


class CollectionJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    request: CollectionRequest
    status: Literal["queued", "running", "retrying", "succeeded", "partial", "failed", "cancelled"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    attempts: int = 0
    result: CollectionResult | None = None
    deduplicated: bool = False
    persistence: Literal["process_memory"] = "process_memory"


class CollectionError(ValueError):
    def __init__(self, category, message):
        self.category = category
        super().__init__(message)


class CollectionJobs:
    """One bounded worker; exact targets never expand to their course/source."""

    def __init__(
        self,
        db,
        engine,
        materials,
        knowledge,
        *,
        enabled=False,
        max_queued=8,
        max_per_hour=20,
        max_history=256,
        retry_delay=2,
    ):
        self.db, self.engine, self.materials, self.knowledge = db, engine, materials, knowledge
        self.enabled = enabled
        self.max_queued, self.max_per_hour, self.max_history = (
            max_queued,
            max_per_hour,
            max_history,
        )
        self.retry_delay = retry_delay
        self.jobs = {}
        self.queue = deque()
        self.keys = {}
        self.fingerprints = {}
        self.deadlines = {}
        self.admitted = deque()
        self.worker = None
        self.closed = False

    def get(self, job_id):
        if job_id not in self.jobs:
            raise CollectionError("not_found", "Unknown job ID (jobs expire when the app exits)")
        return self.jobs[job_id].model_copy(deep=True)

    def submit(self, request: CollectionRequest):
        if not self.enabled:
            raise CollectionError("disabled", "Collection is disabled in the CourseDeck app")
        if self.closed or self.engine.paused:
            raise CollectionError("paused", "Collection is paused for maintenance or shutdown")
        stamp = monotonic()
        payload = request.model_dump_json(exclude={"idempotency_key"})
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        if request.idempotency_key in self.keys:
            old_id, old_fingerprint = self.keys[request.idempotency_key]
            if old_fingerprint != fingerprint:
                raise CollectionError("conflict", "Idempotency key was used for another request")
            return self.get(old_id).model_copy(update={"deduplicated": True})
        previous = self.fingerprints.get(fingerprint)
        if previous:
            old_id, admitted = previous
            old = self.jobs[old_id]
            if old.status in {"queued", "running", "retrying"} or stamp - admitted < 60:
                if request.idempotency_key:
                    self.keys[request.idempotency_key] = (old_id, fingerprint)
                return self.get(old_id).model_copy(update={"deduplicated": True})
        while self.admitted and stamp - self.admitted[0] >= 3600:
            self.admitted.popleft()
        if len(self.admitted) >= self.max_per_hour or len(self.queue) >= self.max_queued:
            raise CollectionError(
                "budget_exhausted", "Collection queue or hourly job budget reached"
            )
        if len(self.jobs) >= self.max_history:
            raise CollectionError("budget_exhausted", "Session job history limit reached")
        # Validate known scope and connection before admitting any remote work.
        self._target(request)
        if request.scope == "source" and not self.engine.reserve_source(request.source):
            raise CollectionError(
                "busy", "Source already has an active or queued reader; retry later"
            )
        job = CollectionJob(
            job_id=uuid4().hex,
            request=request.model_copy(deep=True),
            status="queued",
            created_at=now().isoformat(),
        )
        self.jobs[job.job_id] = job
        self.deadlines[job.job_id] = stamp + request.timeout_seconds
        self.admitted.append(stamp)
        self.fingerprints[fingerprint] = (job.job_id, stamp)
        if request.idempotency_key:
            self.keys[request.idempotency_key] = (job.job_id, fingerprint)
        self.queue.append(job.job_id)
        if self.worker is None or self.worker.done():
            self.worker = self.engine.spawn(self._work())
            self.worker.add_done_callback(self._worker_done)
        return self.get(job.job_id)

    def _worker_done(self, task):
        # A task cancelled before its coroutine first runs never enters _work's
        # try/finally. Still give every admitted job a terminal, inspectable state.
        if task.cancelled() and self.worker is task:
            while self.queue:
                job = self.jobs[self.queue.popleft()]
                self._release(job)
                job.status, job.finished_at = "cancelled", now().isoformat()
                job.result = CollectionResult(
                    outcome="interrupted", message="Collection interrupted before it started"
                )

    def _release(self, job):
        self.deadlines.pop(job.job_id, None)
        if job.request.scope == "source":
            self.engine.release_source(job.request.source)

    def _target(self, request):
        connector = self.engine.connectors.get(request.source)
        if connector is None:
            raise CollectionError("unsupported", "Source has no authorized collection adapter")
        if request.scope == "source":
            return None
        courses = {c["id"]: c for c in self.db.source_courses()}
        if request.scope == "course":
            target = courses.get(request.target_id)
            if target is None or target["provider"] != request.source:
                raise CollectionError(
                    "not_found", "Choose an exact source course ID for this source"
                )
            if connector_capabilities(connector, request.source).materials not in {
                "brightspace_content",
                "classroom_pages",
            }:
                raise CollectionError(
                    "unsupported",
                    "This source has no course-scoped material collector; "
                    "request a source refresh explicitly or a specific task page",
                )
        elif request.scope == "task":
            target = next((t for t in self.db.tasks() if t["id"] == request.target_id), None)
        else:
            target = self.knowledge.get(request.target_id)
            if target and (
                target.get("kind") in {"mail", "email"} or target.get("provider") == "gmail"
            ):
                raise CollectionError("unsupported", "Mailbox remote reading is not exposed")
            if (
                target
                and request.source != "google_classroom"
                and urlsplit(target.get("url") or "").hostname == "docs.google.com"
            ):
                raise CollectionError(
                    "unsupported", "This document requires a different source's credentials"
                )
        if target is None or target.get("provider") != request.source:
            raise CollectionError("not_found", "Target is not cached under the requested source")
        if request.scope in {"task", "document"}:
            cid = (
                target.get("source_course_id")
                if request.scope == "task"
                else target.get("course_id")
            )
            if cid not in courses or courses[cid]["provider"] != request.source:
                raise CollectionError(
                    "unsupported", "Target has no verified source course association"
                )
            if request.source not in PROVIDERS:
                raise CollectionError("unsupported", "This source has no bounded page reader")
            active = getattr(connector, "active", connector)
            if not getattr(active, "browser", None):
                raise CollectionError(
                    "unsupported", "This target requires an existing browser connection"
                )
        return target

    def _connected(self, source):
        if self.engine.paused:
            raise CollectionError("paused", "Collection is paused for maintenance")
        connector = self.engine.connectors[source]
        if connector.connection_status() != "connected":
            raise CollectionError("auth_required", "Finish source login in CourseDeck, then retry")

    async def _work(self):
        active = None
        try:
            while self.queue:
                active = self.jobs[self.queue.popleft()]
                active.status, active.started_at = "running", now().isoformat()
                try:
                    # The admission deadline includes this service's own queue,
                    # the existing source queue, and every bounded retry delay.
                    remaining = self.deadlines[active.job_id] - monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    async with asyncio.timeout(remaining):
                        for attempt in range(active.request.max_attempts):
                            active.attempts = attempt + 1
                            self._connected(active.request.source)
                            result = await self._execute(active.request)
                            active.result = result
                            if result.outcome not in {"network_error", "rate_limited"}:
                                break
                            if attempt + 1 < active.request.max_attempts:
                                active.status = "retrying"
                                await asyncio.sleep(self.retry_delay)
                                active.status = "running"
                    active.status = (
                        "succeeded"
                        if active.result.outcome == "success"
                        else "partial"
                        if active.result.outcome == "partial"
                        else "failed"
                    )
                except TimeoutError:
                    active.status = "failed"
                    active.result = CollectionResult(
                        outcome="budget_exhausted",
                        message="Job time budget exhausted; cached data retained",
                    )
                except SourceAccessPaused:
                    active.status = "failed"
                    active.result = CollectionResult(
                        outcome="paused", message="Collection is paused for maintenance"
                    )
                except CollectionError as exc:
                    active.status = "failed"
                    active.result = CollectionResult(
                        outcome=exc.category,
                        message=str(exc),
                        source_url=self._source_url(active.request),
                    )
                except Exception:
                    active.status = "failed"
                    active.result = CollectionResult(
                        outcome="read_error", message="Collection failed; cached data retained"
                    )
                active.finished_at = now().isoformat()
                self._release(active)
                active = None
        except asyncio.CancelledError:
            remaining = ([active] if active is not None else []) + [
                self.jobs[job_id] for job_id in self.queue
            ]
            self.queue.clear()
            for job in remaining:
                self._release(job)
                job.status, job.finished_at = "cancelled", now().isoformat()
                job.result = CollectionResult(
                    outcome="interrupted",
                    message="Collection interrupted by maintenance or shutdown",
                )
            raise

    def _source_url(self, request):
        try:
            target = self._target(request) or {}
            return safe_source_url(target.get("url") or target.get("source_url"))
        except (ValueError, KeyError):
            return None

    async def _execute(self, request):
        target = self._target(request)  # Revalidate queued targets after local edits.
        if request.scope == "source":
            state = await self.engine.collect_source(
                request.source, validate=lambda: self._connected(request.source)
            )
            return CollectionResult(
                outcome=state.get("last_outcome", "error"),
                message="Source refresh finished; inspect source health for coverage",
                complete=False,
                warnings=state.get("warnings", [])[:50],
            )
        if request.scope == "course":
            result = await self.materials.refresh_source(
                request.source, courses=[Course.model_validate(target)]
            )
            return self._material_result(result, target.get("source_url"))
        if request.scope == "task" and request.source == "gradescope":
            return await self._gradescope(target)
        return await self._page(request, target)

    async def _gradescope(self, target):
        from .connectors.gradescope_detail import collect_task_detail, store_task_detail
        from .domain import Task

        connector = self.engine.connectors["gradescope"]
        connector = getattr(connector, "active", connector)
        async with self.engine.source_access("gradescope"):
            self._connected("gradescope")
            async with connector.browser.session(
                connector.config.get("timezone"), read_only=True
            ) as context:
                detail = await collect_task_detail(
                    context, Task.model_validate(target), base_url=connector.base_url
                )
            store_task_detail(self.db, target["id"], detail)
            self.engine.revision += 1
        return CollectionResult(
            outcome=detail.get("outcome", "partial"),
            message="Gradescope detail checked for the selected task; inspect field availability",
            source_url=safe_source_url(target.get("url")),
            warnings=detail.get("warnings", [])[:50],
            truncated=bool(detail.get("truncated")) or len(detail.get("warnings", [])) > 50,
            complete=False,
        )

    @staticmethod
    def _material_result(result, source_url=None):
        documents = result.get("documents", [])
        errors = {str(e.get("category", "read_error")) for e in result.get("errors", [])}
        warnings = list(
            dict.fromkeys(
                result.get("warnings", [])
                + [warning for document in documents for warning in document.get("warnings", [])]
            )
        )
        outcome = (
            "auth_required"
            if "auth_required" in errors
            else "rate_limited"
            if "rate_limited" in errors
            else "network_error"
            if "network_error" in errors
            else "read_error"
            if errors and not documents
            else "partial"
            if errors or warnings or any(not d.get("complete") for d in documents)
            else "success"
            if documents
            else "partial"
        )
        return CollectionResult(
            outcome=outcome,
            message="Only the requested scope was read; coverage may remain partial",
            source_url=safe_source_url(source_url),
            document_ids=[d["id"] for d in documents[:200]],
            document_count=len(documents),
            truncated=len(documents) > 200 or len(warnings) > 50,
            complete=False,
            warnings=warnings[:50]
            or ([] if documents else ["No new readable material was confirmed"]),
        )

    async def _page(self, request, target):
        cid = target.get("source_course_id") if request.scope == "task" else target["course_id"]
        service = BrowserReadContext(
            db=self.db,
            engine=self.engine,
            store=self.knowledge,
            scope=lambda _: [cid],
            link_reader=TaskLinkReader(self.engine),
        )
        task_tools = BrowserReadScope(
            course_id=cid,
            scope=[cid],
            get=lambda task_id: (
                target if request.scope == "task" and task_id == target["id"] else None
            ),
        )
        documents = {}

        def expose(rows, **kwargs):
            documents.update({row["id"]: row for row in rows})
            return rows

        async with BrowserTools(
            service,
            task_tools,
            expose,
            documents={target["id"]: target} if request.scope == "document" else {},
        ) as browser:
            try:
                await browser.run("browser_open", {request.scope + "_id": request.target_id})
            except ValueError as exc:
                # BrowserTools returns safe categories, never raw exception payloads.
                text = str(exc).lower()
                auth = any(word in text for word in ("sign-in", "login", "connect this source"))
                raise CollectionError(
                    "auth_required" if auth else "unsupported",
                    "Finish source login in CourseDeck, then retry"
                    if auth
                    else "This exact target could not be safely read; use its source link",
                ) from None
        result = {"documents": [], "warnings": []}
        for document in documents.values():
            if request.scope == "task":
                document = document | {"source_task_id": target["id"]}
            self.materials.retain_document(document, result)
        return self._material_result(result, target.get("url"))

    async def close(self):
        self.closed = True
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


def build_collection_router(jobs):
    router = APIRouter(prefix="/api/collection", tags=["controlled collection"])

    def guard(request):
        try:
            loopback = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            loopback = False
        if (
            not loopback
            or request.headers.get("origin") is not None
            or request.headers.get("x-coursedeck-collection") != "1"
        ):
            raise HTTPException(403, "Collection requires an explicit local agent request")
        if not jobs.enabled:
            raise HTTPException(403, "Collection is disabled in the CourseDeck app")

    @router.post("/jobs", response_model=CollectionJob, status_code=202)
    async def submit(value: CollectionRequest, request: Request):
        guard(request)
        try:
            return jobs.submit(value)
        except CollectionError as exc:
            status = {
                "not_found": 404,
                "conflict": 409,
                "paused": 409,
                "busy": 409,
                "disabled": 403,
                "budget_exhausted": 429,
            }.get(exc.category, 422)
            raise HTTPException(status, {"category": exc.category, "message": str(exc)}) from None

    @router.get("/jobs/{job_id}", response_model=CollectionJob)
    async def status(job_id: str, request: Request):
        guard(request)
        try:
            return jobs.get(job_id)
        except CollectionError as exc:
            raise HTTPException(404, {"category": exc.category, "message": str(exc)}) from None

    return router
