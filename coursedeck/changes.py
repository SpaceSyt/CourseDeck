"""Transaction-local source change tracking; local task edits never become source events."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .knowledge import safe_source_url
from .task_state import (
    DONE_STATUSES,
    OPEN_STATUSES,
    TASK_FIELDS,
    FieldAvailability,
    field_availability,
    unread_fields,
)

# Match frontend/src/tasks.ts: Classroom's normalized `returned` belongs in Done.
COMPLETED = DONE_STATUSES
OPEN = OPEN_STATUSES
FIELDS = set(TASK_FIELDS)
KINDS = {
    "added",
    "deadline_earlier",
    "deadline_delayed",
    "deadline_changed",
    "reopened",
    "status_changed",
    "status_confirmed",
    "body_changed",
    "title_changed",
    "missing",
    "unrefreshed",
    "restored",
    "fields_unavailable",
    "fields_restored",
}


def _state(row, finished_at):
    payload = json.loads(row["payload"])
    unavailable = unread_fields(payload.get("raw_data", {}))
    status = payload.get("submission_status") or "unknown"
    if field_availability(payload, "submission_status") != FieldAvailability.KNOWN:
        unavailable.add("submission_status")
    availability = (
        "missing"
        if row["missing_count"]
        else ("present" if row["last_seen_at"] == finished_at else "unconfirmed")
    )
    return {
        key: payload.get(key) for key in ("title", "description", "due_at", "submission_status")
    } | {
        "availability": availability,
        "unavailable_fields": sorted(unavailable),
        "status_known": availability == "present" and "submission_status" not in unavailable,
        "last_known_status": status if status != "unknown" else None,
        "last_seen_at": row["last_seen_at"],
    }


def initialize(connection):
    from .migrations import initialize_component

    initialize_component(connection, "changes")
    if connection.execute("SELECT 1 FROM change_settings WHERE key='baseline_ready'").fetchone():
        return
    finished = {
        row["provider"]: json.loads(row["payload"]).get("last_finished_sync")
        for row in connection.execute("SELECT * FROM connector_states")
    }
    # The first upgrade adopts existing cache as a baseline, without generating additions.
    for row in connection.execute("SELECT * FROM tasks").fetchall():
        projection = _state(row, finished.get(row["provider"]) or row["last_seen_at"])
        connection.execute(
            "INSERT OR IGNORE INTO task_change_baselines VALUES (?, ?)",
            (row["id"], json.dumps(projection)),
        )
    connection.execute("INSERT INTO change_settings VALUES ('baseline_ready', '1')")


def record_sync(connection, provider, observed_at, outcome):
    rows = connection.execute("SELECT * FROM tasks WHERE provider=?", (provider,)).fetchall()
    valid = outcome in {"success", "partial"}
    for row in rows:
        current = _state(row, observed_at)
        baseline = connection.execute(
            "SELECT payload FROM task_change_baselines WHERE task_id=?", (row["id"],)
        ).fetchone()
        previous = json.loads(baseline[0]) if baseline else None
        if previous and not current["status_known"]:
            current["last_known_status"] = previous.get("last_known_status")
        payload = json.loads(row["payload"])
        events = []

        def event(kind, before, after, severity="info", sink=events):
            sink.append((kind, before, after, severity))

        if previous is None:
            if valid:
                event(
                    "added",
                    None,
                    {
                        key: current[key]
                        for key in ("title", "due_at", "submission_status", "status_known")
                    },
                    "critical",
                )
        else:
            if current["availability"] != previous["availability"]:
                event(
                    {"present": "restored", "missing": "missing", "unconfirmed": "unrefreshed"}[
                        current["availability"]
                    ],
                    previous["availability"],
                    current["availability"],
                    "critical" if current["availability"] == "missing" else "info",
                )
            # Failed or incomplete reads cannot assert a source field changed.
            if valid and current["availability"] == "present":
                unavailable = set(current["unavailable_fields"])
                was_unavailable = set(previous["unavailable_fields"])
                lost, restored = unavailable - was_unavailable, was_unavailable - unavailable
                if lost:
                    event(
                        "fields_unavailable",
                        sorted(was_unavailable),
                        sorted(unavailable),
                        "critical" if lost & {"due_at", "submission_status"} else "info",
                    )
                if restored:
                    event("fields_restored", sorted(was_unavailable), sorted(unavailable))
                if "due_at" not in unavailable and current["due_at"] != previous["due_at"]:
                    kind, severity = "deadline_changed", "critical"
                    if current["due_at"] and previous["due_at"]:
                        before = datetime.fromisoformat(previous["due_at"])
                        after = datetime.fromisoformat(current["due_at"])
                        if after < before:
                            kind, severity = "deadline_earlier", "critical"
                        elif after > before:
                            kind, severity = "deadline_delayed", "info"
                        else:
                            kind = None  # Equivalent instants with different timezone spelling.
                    elif previous["due_at"]:
                        severity = "critical"
                    if kind:
                        event(kind, previous["due_at"], current["due_at"], severity)
                if current["status_known"]:
                    old_status = previous.get("last_known_status")
                    new_status = current["submission_status"]
                    if old_status in COMPLETED and new_status in OPEN:
                        event("reopened", old_status, new_status, "critical")
                    elif old_status != new_status:
                        event(
                            "status_changed" if old_status else "status_confirmed",
                            old_status,
                            new_status,
                        )
                    elif not previous["status_known"]:
                        event("status_confirmed", previous["submission_status"], new_status)
                if (
                    "description" not in unavailable
                    and current["description"] != previous["description"]
                ):
                    if (current["description"] or "").replace("\r\n", "\n") != (
                        previous["description"] or ""
                    ).replace("\r\n", "\n"):
                        event("body_changed", previous["description"], current["description"])
                if current["title"] != previous["title"]:
                    event("title_changed", previous["title"], current["title"])
        evidence = {
            "url": safe_source_url(payload.get("url")),
            "source_updated_at": payload.get("source_updated_at"),
            "last_seen_at": row["last_seen_at"],
            "outcome": str(outcome),
            "availability": current["availability"],
            "unavailable_fields": current["unavailable_fields"],
        }
        for kind, before, after, severity in events:
            connection.execute(
                """INSERT INTO task_changes
                (task_id,provider,course_id,title,kind,severity,before_value,after_value,evidence,observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    provider,
                    row["course_id"],
                    payload["title"],
                    kind,
                    severity,
                    json.dumps(before),
                    json.dumps(after),
                    json.dumps(evidence),
                    observed_at,
                ),
            )
        connection.execute(
            "INSERT OR REPLACE INTO task_change_baselines VALUES (?, ?)",
            (row["id"], json.dumps(current)),
        )


def _courses(db, course_id=None):
    canonical = db.resolve_course_alias(course_id) if course_id else None
    if course_id and not canonical:
        raise ValueError("Course not found")
    return {
        source: course
        for course in db.courses()
        if not course.get("disabled")
        and not course.get("deleted")
        and (not canonical or course["id"] == canonical)
        for source in course["source_course_ids"]
    }


def _where(courses, provider=None, kind=None, unread=False, critical=False, query=""):
    clauses, params = ["course_id IN (" + ",".join("?" for _ in courses) + ")"], list(courses)
    if not courses:
        return "0", []
    for field, value in (("provider", provider), ("kind", kind)):
        if value:
            clauses.append(field + "=?")
            params.append(value)
    if unread:
        clauses.append("read_at IS NULL")
    if critical:
        clauses.append("severity='critical'")
    if query.strip():
        text = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(
            "(title LIKE ? ESCAPE '\\' OR "
            "CAST(json_extract(before_value, '$') AS TEXT) LIKE ? ESCAPE '\\' OR "
            "CAST(json_extract(after_value, '$') AS TEXT) LIKE ? ESCAPE '\\')"
        )
        params.extend(["%" + text + "%"] * 3)
    return " AND ".join(clauses), params


def summary(db):
    where, params = _where(_courses(db), unread=True)
    with db.connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS unread_count, "
            "COALESCE(SUM(severity='critical'),0) AS critical_unread_count FROM task_changes WHERE "
            + where,
            params,
        ).fetchone()
    return dict(row)


def list_changes(
    db,
    *,
    course_id=None,
    provider=None,
    kind=None,
    unread=False,
    critical=False,
    query="",
    offset=0,
    limit=50,
):
    if kind and kind not in KINDS:
        raise ValueError("Unknown change type")
    courses = _courses(db, course_id)
    where, params = _where(courses, provider, kind, unread, critical, query)
    with db.connection() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM task_changes WHERE " + where, params
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM task_changes WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    changes = []
    for row in rows:
        change = dict(row)
        change.update(
            source_course_id=row["course_id"],
            course_id=courses[row["course_id"]]["id"],
            before=json.loads(change.pop("before_value")),
            after=json.loads(change.pop("after_value")),
            evidence=json.loads(row["evidence"]),
        )
        changes.append(change)
    return {"changes": changes, "total": total} | summary(db)


class ReadChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool


class ReadChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] | None = Field(default=None, max_length=500)


def build_changes_router(db):
    router = APIRouter(prefix="/api/changes")

    @router.get("")
    async def index(
        course_id: str | None = None,
        provider: str | None = None,
        kind: str | None = None,
        unread: bool = False,
        critical: bool = False,
        query: str = Query("", max_length=2000),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        try:
            return list_changes(
                db,
                course_id=course_id,
                provider=provider,
                kind=kind,
                unread=unread,
                critical=critical,
                query=query,
                offset=offset,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.patch("/{change_id}")
    async def read(change_id: int, value: ReadChange):
        from .domain import now

        with db.connection() as connection:
            result = connection.execute(
                "UPDATE task_changes SET read_at=? WHERE id=?",
                (now().isoformat() if value.read else None, change_id),
            )
            if not result.rowcount:
                raise HTTPException(404, "Change not found")
        return summary(db)

    @router.post("/read")
    async def read_many(value: ReadChanges):
        from .domain import now

        where, params = _where(_courses(db), unread=True)
        if value.ids is not None:
            if not value.ids:
                return summary(db)
            where += " AND id IN (" + ",".join("?" for _ in value.ids) + ")"
            params.extend(value.ids)
        with db.connection() as connection:
            connection.execute(
                "UPDATE task_changes SET read_at=? WHERE " + where, [now().isoformat(), *params]
            )
        return summary(db)

    return router
