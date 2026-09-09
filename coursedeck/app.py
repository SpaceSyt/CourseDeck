import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .connectors.registry import build_connectors
from .db import Database
from .domain import Settings, now
from .gmail_browser import GmailBrowser
from .mail import CustomTaskInput, MailPatch, MailRule, MailStore, save_custom_task
from .sync import SyncEngine


class LocalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hidden: bool | None = None
    dismissed: bool | None = None
    pinned: bool | None = None
    note: str | None = Field(default=None, max_length=10000)
    priority: int | None = Field(default=None, ge=0, le=3)


class CourseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    provider: str
    remote_course_id: str | None = None
    source_course_ids: list[str] | None = Field(default=None, max_length=100)


class CourseSources(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    source_course_ids: list[str] = Field(max_length=100)
    alias: str | None = Field(default=None, max_length=200)


class CoursePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    alias: str | None = Field(default=None, max_length=200)
    disabled: bool = False
    deleted: bool = False


class CourseBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_course_id: str = Field(min_length=1)


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.environ.get("COURSEDECK_DATA_DIR", "data")).resolve()
    db = Database(data_dir / "coursedeck.sqlite3")
    engine = SyncEngine(db, build_connectors(db, data_dir))
    mail = MailStore(db)
    gmail = GmailBrowser(db, data_dir)

    @asynccontextmanager
    async def lifespan(app):
        mail_poll = asyncio.create_task(gmail.poll())
        if db.settings().startup_sync:
            engine.spawn(engine.sync_all())
            gmail.spawn()
        yield
        mail_poll.cancel()
        await asyncio.gather(mail_poll, return_exceptions=True)
        await gmail.close()
        await engine.close()

    app = FastAPI(title="CourseDeck", lifespan=lifespan)
    app.state.db, app.state.engine = db, engine
    app.state.gmail = gmail
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

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
        return {"service": "coursedeck", "status": "connected", "time": now().isoformat()}

    @app.post("/api/courses", status_code=201)
    async def add_course(course: CourseCreate):
        connector(course.provider)
        try:
            key = db.add_course(
                course.name, course.provider, course.remote_course_id, course.source_course_ids
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": key, "message": "Course added."}

    @app.put("/api/courses/{course_id}/sources")
    async def update_course_sources(course_id: str, value: CourseSources):
        try:
            options = {"alias": value.alias} if "alias" in value.model_fields_set else {}
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
    async def snapshot():
        return {
            "tasks": db.tasks(),
            "courses": db.courses(),
            "source_courses": db.source_courses(),
            "revision": engine.revision,
            "settings": db.settings(),
            "sources": [
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
            ],
        }

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
    ):
        return mail.list(deleted, ignored, offset, limit) | {"connection": gmail.status()}

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
            return db.patch_local(task_id, patch.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc

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
        from .domain import now

        folder = data_dir / "backups"
        folder.mkdir(exist_ok=True)
        path = folder / f"coursedeck-{now().strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        db.backup(path)
        return {"message": f"Backup saved to {path}"}

    frontend = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/{path:path}")
        async def index(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(frontend / "index.html")

    return app
