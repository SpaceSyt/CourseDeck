import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .associations import AssociationStore
from .associations import build_router as build_associations_router
from .changes import build_changes_router
from .changes import summary as changes_summary
from .chat import build_chat_router
from .connectors.registry import build_connectors
from .db import Database
from .desktop import build_desktop_router
from .domain import Settings, now
from .gmail_browser import GmailBrowser
from .knowledge import KnowledgeStore
from .library import material_sync
from .mail import (
    CustomTaskInput,
    MailPatch,
    MailRule,
    MailStore,
    build_mail_rules_router,
    save_custom_task,
)
from .maintenance import MaintenanceGate
from .materials import MaterialCollector
from .recovery import RecoveryManager, build_recovery_router
from .revisions import DatabaseRevision
from .runtime import data_directory
from .sync import SyncEngine
from .task_edits import TaskEdits
from .task_history import build_task_history_router


class LocalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hidden: bool | None = None
    dismissed: bool | None = None
    pinned: bool | None = None
    note: str | None = Field(default=None, max_length=10000)
    priority: int | None = Field(default=None, ge=0, le=3)
    expected_version: str | None = Field(default=None, min_length=64, max_length=64)


class CourseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    provider: str
    remote_course_id: str | None = None
    source_course_ids: list[str] | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class CourseSources(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    source_course_ids: list[str] = Field(max_length=100)
    alias: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class CoursePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    alias: str | None = Field(default=None, max_length=200)
    disabled: bool = False
    deleted: bool = False
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class CourseMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_ids: list[str] = Field(min_length=1, max_length=100)


class CourseBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_course_id: str = Field(min_length=1)


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = data_directory(data_dir)
    db = Database(data_dir / "coursedeck.sqlite3")
    engine = SyncEngine(db, build_connectors(db, data_dir))
    knowledge = KnowledgeStore(data_dir / "knowledge.sqlite3")
    if not (data_dir / "backups" / "restore-pending.json").exists():
        knowledge.index_tasks(db)
    materials = MaterialCollector(db, engine, data_dir, knowledge_store=knowledge)
    engine.after_sync = material_sync(db, materials, knowledge)
    mail = MailStore(db)
    gmail = GmailBrowser(db, data_dir)
    associations = AssociationStore(db, knowledge, mail)
    recovery = RecoveryManager(data_dir, db, knowledge)
    snapshot_revision = DatabaseRevision(db.path)
    snapshot_session = uuid4().hex

    def close_readers():
        associations.close()
        knowledge.close()
        snapshot_revision.close()

    gate = MaintenanceGate(lambda: recovery.journal.exists())
    background = {"started": False, "mail_poll": None, "source_poll": None}

    def start_background():
        if background["started"] and not recovery.journal.exists():
            if background["mail_poll"] is None or background["mail_poll"].done():
                background["mail_poll"] = asyncio.create_task(gmail.poll())
            if background["source_poll"] is None or background["source_poll"].done():
                background["source_poll"] = engine.spawn(engine.poll())

    async def stop_mail():
        poll = background["mail_poll"]
        if poll is not None:
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            background["mail_poll"] = None
        await gmail.close()

    @asynccontextmanager
    async def maintenance():
        async with gate.maintenance():
            browsers = [gmail.browser] + [
                getattr(getattr(source, "active", source), "browser", None)
                for source in engine.connectors.values()
            ]
            if any(browser and browser.interactive for browser in browsers):
                raise HTTPException(409, "Finish source sign-in before backing up or restoring")
            try:
                await stop_mail()
                async with (
                    gmail.lock,
                    engine.maintenance(can_resume=lambda: not recovery.journal.exists()),
                ):
                    close_readers()
                    yield
                    materials.reset_runtime_cache()
                    if not recovery.journal.exists():
                        knowledge.index_tasks(db)
                    engine.revision += 1
            finally:
                start_background()

    @asynccontextmanager
    async def lifespan(app):
        try:
            background["started"] = True
            start_background()
            if db.settings().startup_sync and not recovery.journal.exists():
                engine.spawn(engine.sync_all(automatic=True))
                gmail.spawn()
            yield
        finally:
            background["started"] = False
            try:
                await stop_mail()
            finally:
                try:
                    await engine.close()
                finally:
                    close_readers()

    app = FastAPI(title="CourseDeck", lifespan=lifespan)
    app.state.db, app.state.engine = db, engine
    app.state.knowledge = knowledge
    app.state.gmail = gmail
    app.state.associations = associations
    app.state.recovery = recovery
    app.state.maintenance_gate = gate
    app.state.close_readers = close_readers
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.include_router(
        build_chat_router(
            db,
            engine,
            data_dir,
            material_fetch=materials.fetch,
            knowledge_store=knowledge,
            gmail=gmail,
        )
    )
    app.include_router(build_changes_router(db))
    app.include_router(build_task_history_router(db))
    app.include_router(build_associations_router(associations))
    app.include_router(build_mail_rules_router(db))
    app.include_router(build_recovery_router(recovery, maintenance))
    app.include_router(build_desktop_router(data_dir))

    @app.exception_handler(ValueError)
    async def invalid_config(request, exc):
        return JSONResponse(
            {"detail": "Invalid configuration or login response. Check source setup."}, 400
        )

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        return JSONResponse(
            {
                "detail": "Operation failed. Check the OS credential store, "
                "browser installation, and source configuration."
            },
            500,
        )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-coursedeck") != "1" or (
                origin and origin != str(request.base_url).rstrip("/")
            ):
                return JSONResponse(
                    {"detail": "Only the local CourseDeck UI may make changes"}, 403
                )
        protected = request.url.path.startswith("/api/") and not (
            request.url.path in {"/api/heartbeat", "/api/backup"}
            or request.url.path.startswith("/api/recovery/")
        )
        if protected:
            async with gate.request() as admitted:
                if not admitted:
                    return JSONResponse(
                        {"detail": "Database recovery is in progress or requires attention"},
                        503,
                        headers={"Cache-Control": "no-store", "Retry-After": "3"},
                    )
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def connector(key):
        if key not in engine.connectors:
            raise HTTPException(404, "Unknown source")
        return engine.connectors[key]

    @app.get("/api/heartbeat")
    async def heartbeat():
        return {
            "service": "coursedeck",
            "status": "connected",
            "time": now().isoformat(),
            "maintenance": gate.busy,
            "recovery_required": recovery.journal.exists(),
        }

    @app.get("/api/recovery/status")
    async def recovery_status():
        return {"maintenance": gate.busy, "recovery_required": recovery.journal.exists()}

    @app.post("/api/courses", status_code=201)
    async def add_course(course: CourseCreate):
        connector(course.provider)
        try:
            key = db.add_course(
                course.name,
                course.provider,
                course.remote_course_id,
                course.source_course_ids,
                color=course.color,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": key, "message": "Course added."}

    @app.put("/api/courses/{course_id}/sources")
    async def update_course_sources(course_id: str, value: CourseSources):
        try:
            options = {"alias": value.alias} if "alias" in value.model_fields_set else {}
            if "color" in value.model_fields_set:
                options["color"] = value.color
            return {
                "id": db.update_course_sources(
                    course_id, value.name, value.source_course_ids, **options
                )
            }
        except KeyError as exc:
            raise HTTPException(404, "Course not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/courses/{course_id}")
    async def patch_course(course_id: str, value: CoursePatch):
        try:
            db.patch_course(course_id, value.model_dump(exclude_unset=True))
        except KeyError as exc:
            raise HTTPException(404, "Course not found") from exc
        return {"ok": True}

    @app.post("/api/courses/{course_id}/merge")
    async def merge_courses(course_id: str, value: CourseMerge):
        try:
            return {"id": db.merge_courses(course_id, value.course_ids)}
        except KeyError as exc:
            raise HTTPException(404, "Course not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/courses/{course_id}")
    async def delete_course(course_id: str):
        try:
            db.patch_course(course_id, {"deleted": True})
        except KeyError as exc:
            raise HTTPException(404, "Course not found") from exc
        return {"ok": True}

    @app.patch("/api/courses/{workspace_id}/binding")
    async def bind_course(workspace_id: str, binding: CourseBinding):
        try:
            db.bind_course(workspace_id, binding.remote_course_id)
        except KeyError as exc:
            raise HTTPException(404, "Course not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"message": "Source course linked."}

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        before = snapshot_revision.token()
        sources = [
            {
                "key": k,
                "name": c.display_name,
                "description": c.description,
                "manual_login": c.manual_login,
                "configuration_fields": c.configuration_fields,
                "configuration_values": c.configuration_values,
                "status": c.connection_status(),
                "syncing": k in engine.running,
                **{
                    field: value
                    for field, value in db.state(k).items()
                    if field
                    in {
                        "last_outcome",
                        "last_successful_sync",
                        "last_attempted_sync",
                        "warnings",
                        "metadata",
                    }
                },
            }
            for k, c in engine.connectors.items()
        ]
        token = hashlib.sha256(
            json.dumps(
                [snapshot_session, before, engine.revision, sources], sort_keys=True
            ).encode()
        ).hexdigest()
        etag = '"' + token + '"'
        stable = before == snapshot_revision.token()
        if stable and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        value = {
            "tasks": db.tasks(),
            "courses": db.courses(),
            "source_courses": db.source_courses(),
            "revision": engine.revision,
            "changes": changes_summary(db),
            "settings": db.settings(),
            "sources": sources,
        }
        # A concurrent writer invalidates this observation; never certify a mixed snapshot.
        headers = {"ETag": etag} if stable and before == snapshot_revision.token() else {}
        return JSONResponse(jsonable_encoder(value), headers=headers)

    @app.post("/api/custom-tasks", status_code=201)
    async def create_custom_task(value: CustomTaskInput):
        try:
            return {"id": save_custom_task(db, value)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/custom-tasks/{key}")
    async def edit_custom_task(key: str, value: CustomTaskInput):
        try:
            return {"id": save_custom_task(db, value, key)}
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/mail")
    async def mail_list(
        deleted: bool = False,
        ignored: bool = False,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        attention_only: bool = False,
        order: Literal["newest", "attention"] = "newest",
    ):
        value = await run_in_threadpool(
            mail.list, deleted, ignored, offset, limit, attention_only=attention_only, order=order
        )
        return value | {"connection": gmail.status()}

    @app.get("/api/mail/rules")
    async def mail_rules():
        return mail.rules()

    @app.post("/api/mail/rules", status_code=201)
    async def add_mail_rule(rule: MailRule):
        try:
            return {"id": mail.add_rule(rule)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/mail/rules/{key}")
    async def delete_mail_rule(key: str):
        try:
            mail.delete_rule(key)
        except KeyError as exc:
            raise HTTPException(404, "Rule not found") from exc
        return {"ok": True}

    @app.put("/api/mail/rules/{key}")
    async def update_mail_rule(key: str, rule: MailRule):
        try:
            return {"id": mail.update_rule(key, rule)}
        except KeyError as exc:
            raise HTTPException(404, "Rule not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/mail/rules/reset-defaults")
    async def reset_mail_rules():
        mail.reset_defaults()
        return {"ok": True}

    @app.post("/api/mail/connection/{operation}")
    async def mail_connection(operation: str):
        try:
            if operation == "connect":
                return await gmail.connect()
            if operation == "finish-login":
                return await gmail.finish_login()
            if operation == "disconnect":
                return await gmail.disconnect()
            if operation == "sync":
                if gmail.status()["status"] != "connected":
                    raise ValueError("Connect Gmail first")
                gmail.spawn()
                return {"queued": True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        raise HTTPException(404, "Unknown operation")

    @app.get("/api/mail/messages/{key}")
    async def mail_detail(key: str):
        try:
            return mail.get(key)
        except KeyError as exc:
            raise HTTPException(404, "Email not found") from exc

    @app.patch("/api/mail/messages/{key}")
    async def patch_mail(key: str, value: MailPatch):
        try:
            return mail.patch(key, value.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(404, "Email not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/sync", status_code=202)
    async def sync_all():
        engine.spawn(engine.sync_all())
        return {"queued": True}

    @app.post("/api/sources/{key}/sync", status_code=202)
    async def sync_one(key: str):
        connector(key)
        engine.spawn(engine.sync_one(key))
        return {"queued": True}

    @app.post("/api/sources/{key}/connect")
    async def connect(key: str, request: Request):
        c = connector(key)
        if engine.locks[key].locked():
            raise HTTPException(409, "This source is busy")
        async with engine.locks[key]:
            result = await c.connect(str(request.base_url) + f"api/oauth/{key}/callback")
        if c.connection_status() == "connected":
            engine.spawn(engine.sync_one(key))
        return result

    @app.put("/api/sources/{key}/configure")
    async def configure(key: str, config: dict):
        c = connector(key)
        if engine.locks[key].locked():
            raise HTTPException(409, "This source is busy")
        async with engine.locks[key]:
            await c.configure(config)
        return {"message": "Configuration saved. Connect this source when ready."}

    @app.post("/api/sources/{key}/finish-login")
    async def finish_login(key: str):
        c = connector(key)
        if engine.locks[key].locked():
            raise HTTPException(409, "This source is busy")
        async with engine.locks[key]:
            result = await c.finish_login()
        if c.connection_status() == "connected":
            engine.spawn(engine.sync_one(key))
        return result

    @app.get("/api/oauth/{key}/callback")
    async def oauth_callback(key: str, request: Request):
        c = connector(key)
        async with engine.locks[key]:
            await c.authorization_callback(dict(request.query_params))
        engine.spawn(engine.sync_one(key))
        return HTMLResponse(
            "<h2>Connected to CourseDeck</h2>"
            "<p>You can close this tab and return to your local workspace.</p>"
        )

    @app.post("/api/sources/{key}/disconnect")
    async def disconnect(key: str):
        c = connector(key)
        if engine.locks[key].locked():
            raise HTTPException(409, "Wait for this source to finish syncing")
        async with engine.locks[key]:
            await c.disconnect()
        return {"message": "Disconnected. Cached tasks and local notes are retained."}

    @app.patch("/api/tasks/{task_id:path}/local")
    async def patch_local(task_id: str, patch: LocalPatch):
        try:
            result = TaskEdits(db).patch_local(
                task_id,
                patch.model_dump(exclude_none=True, exclude={"expected_version"}),
                expected_version=patch.expected_version,
            )
            return result["after"]
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.put("/api/settings")
    async def settings(value: Settings):
        db.save_settings(value)
        return value

    @app.get("/api/history")
    async def history():
        return db.history()

    @app.get("/api/debug")
    async def debug():
        # Excludes raw data, school URLs, user notes, credentials and browser content.
        return {
            "version": "0.1.0",
            "data_location": str(data_dir),
            "history": db.history(),
            "task_count": len(db.tasks()),
            "course_count": len(db.courses()),
        }

    @app.post("/api/backup")
    async def backup():
        async with maintenance():
            manifest = recovery.create_backup()
        return {"message": "Task and knowledge backup saved", "backup": manifest}

    frontend = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/{path:path}")
        async def index(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(frontend / "index.html")

    return app
