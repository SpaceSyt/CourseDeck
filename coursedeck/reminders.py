"""Local, deterministic deadline reminders. Claiming a batch never sends a notification.

UTC instants drive scheduling; the configured IANA zone only drives quiet hours.
SQLite serializes claims (including multiple tray processes) and persists dedupe.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from .domain import now


def instant(value):
    if not value:
        return None
    try:
        value = datetime.fromisoformat(value)
        return value.astimezone(UTC) if value.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def quiet_at(settings, at):
    preferences = settings.reminders
    if not preferences.quiet_enabled:
        return False
    clock = at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
    start, end = preferences.quiet_start, preferences.quiet_end
    if start == end:
        return False
    return start <= clock < end if start < end else clock >= start or clock < end


def read_runtime(connection, key, default=None):
    row = connection.execute("SELECT payload FROM reminder_runtime WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write_runtime(connection, key, value):
    connection.execute(
        "INSERT OR REPLACE INTO reminder_runtime VALUES (?,?)", (key, json.dumps(value))
    )


class ReminderEngine:
    def __init__(self, db, clock=now):
        self.db = db
        self.clock = clock

    def status(self):
        with self.db.connection() as connection:
            return {
                "enabled": self.db.settings().reminders.enabled,
                "snoozed_until": read_runtime(connection, "snoozed_until"),
                "last_batch": read_runtime(connection, "last_batch"),
            }

    def snooze(self):
        until = (self.clock() + timedelta(minutes=30)).isoformat()
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            write_runtime(connection, "snoozed_until", until)
            batch = read_runtime(connection, "last_batch", {})
            # Keep identity and due instant, then recheck current visibility/completion
            # when the snooze expires; never replay the old notification blindly.
            write_runtime(connection, "snoozed_items", batch.get("items", []))
        return {"snoozed_until": until}

    def claim(self):
        at = self.clock().astimezone(UTC)
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            settings = self.db.settings()
            config = settings.reminders
            empty = {"batch_id": None, "count": 0, "items": [], "timezone": settings.timezone}
            if not config.enabled:
                return empty
            # The write reservation prevents task/settings writers from changing the
            # projections read through Database's existing public read methods.
            tasks = self.db.tasks()
            courses = {course["id"]: course for course in self.db.courses()}
            active = read_runtime(connection, "active", False)
            cursor = read_runtime(connection, "change_cursor", 0)
            changes = connection.execute(
                "SELECT id,task_id,after_value,observed_at FROM task_changes "
                "WHERE id>? AND kind='deadline_earlier' ORDER BY id",
                (cursor,),
            ).fetchall()
            latest = connection.execute("SELECT COALESCE(MAX(id),0) FROM task_changes").fetchone()[
                0
            ]
            write_runtime(connection, "change_cursor", latest)
            write_runtime(connection, "active", True)
            earlier = {}
            for change in changes:
                earlier[change["task_id"]] = (
                    instant(json.loads(change["after_value"])),
                    change["observed_at"],
                )
            snoozed_until = instant(read_runtime(connection, "snoozed_until"))
            snoozed = {
                item["task_id"]: item for item in read_runtime(connection, "snoozed_items", [])
            }
            last_claim = instant(read_runtime(connection, "last_claim"))
            blocked = (
                quiet_at(settings, at)
                or (snoozed_until is not None and at < snoozed_until)
                or (last_claim is not None and at < last_claim + timedelta(minutes=5))
            )
            items = []
            states = {}
            for task in tasks:
                task_id = task["id"]
                due = instant(task.get("due_at"))
                row = connection.execute(
                    "SELECT payload FROM reminder_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                state = json.loads(row[0]) if row else {}
                old_due = instant(state.get("due"))
                normalized = due.isoformat() if due else None
                if state.get("due") != normalized:
                    state = {
                        "due": normalized,
                        "sent": [],
                        "earlier": bool(active and old_due and due and due < old_due),
                        "earlier_at": at.isoformat(),
                    }
                if not active:
                    state["earlier"] = False
                source_change = earlier.get(task_id)
                if (
                    not task["local"].get("due_override")
                    and due
                    and source_change
                    and source_change[0] == due
                ):
                    state["earlier"] = True
                    state["earlier_at"] = source_change[1]
                earlier_at = instant(state.get("earlier_at"))
                fresh_earlier = (
                    config.deadline_earlier
                    and state.get("earlier")
                    and earlier_at
                    and at - earlier_at <= timedelta(hours=24)
                )
                states[task_id] = state
                course = courses.get(task.get("course_id"), {})
                if (
                    task["local"].get("hidden")
                    or task["is_completed"]
                    or course.get("deleted")
                    or course.get("disabled")
                    or not due
                    or (due <= at and not fresh_earlier)
                ):
                    state["earlier"] = False
                    snoozed.pop(task_id, None)
                    continue
                # A snooze for a previous deadline cannot resurrect that old schedule.
                replay = snoozed.get(task_id)
                if replay and instant(replay["due_at"]) != due:
                    snoozed.pop(task_id, None)
                    replay = None
                remaining = due - at
                kinds = []
                if config.deadline_earlier and state.get("earlier"):
                    kinds.append("deadline_earlier")
                if (
                    config.lead_2h
                    and timedelta(0) < remaining <= timedelta(hours=2)
                    and "2h" not in state["sent"]
                ):
                    kinds.append("2h")
                elif (
                    config.lead_24h
                    and timedelta(0) < remaining <= timedelta(hours=24)
                    and "24h" not in state["sent"]
                ):
                    kinds.append("24h")
                if replay and snoozed_until is not None and at >= snoozed_until:
                    kinds.append("snoozed")
                if not kinds or blocked:
                    continue
                warning = self._warning(task, at)
                items.append(
                    {
                        "task_id": task_id,
                        "title": task["title"],
                        "due_at": normalized,
                        "kinds": kinds,
                        "warning": warning,
                    }
                )
                state["sent"] = sorted(
                    set(state["sent"]) | set(kinds) - {"snoozed", "deadline_earlier"}
                )
                if "2h" in kinds:
                    # After sleep/restart, the urgent reminder supersedes missed 24h.
                    state["sent"] = sorted(set(state["sent"]) | {"24h"})
                state["earlier"] = False
                snoozed.pop(task_id, None)
            connection.executemany(
                "INSERT OR REPLACE INTO reminder_tasks VALUES (?,?)",
                [(key, json.dumps(value)) for key, value in states.items()],
            )
            # Remove only orphaned scheduling state, never task or change records.
            existing = {row[0] for row in connection.execute("SELECT task_id FROM reminder_tasks")}
            connection.executemany(
                "DELETE FROM reminder_tasks WHERE task_id=?",
                [(key,) for key in existing - states.keys()],
            )
            snoozed = {key: item for key, item in snoozed.items() if key in states}
            write_runtime(connection, "snoozed_items", list(snoozed.values()))
            if snoozed_until is not None and at >= snoozed_until and not snoozed:
                write_runtime(connection, "snoozed_until", None)
            if not items:
                return empty
            items.sort(key=lambda item: (item["due_at"], item["task_id"]))
            batch = {
                "batch_id": uuid4().hex,
                "count": len(items),
                "items": items,
                "timezone": settings.timezone,
            }
            write_runtime(connection, "last_claim", at.isoformat())
            write_runtime(connection, "last_batch", batch)
            return batch

    @staticmethod
    def _warning(task, at):
        warnings = []
        if task["provider"] != "custom":
            seen = instant(task.get("last_seen_at"))
            if (
                not seen
                or at - seen > timedelta(hours=24)
                or task.get("source_availability") != "present"
                or task.get("field_availability", {}).get("due_at") != "known"
            ):
                warnings.append("Cached deadline; verify on the source platform")
            if task.get("source_state") == "unknown":
                warnings.append("Submission status unknown")
        return "; ".join(warnings) or None


def build_reminders_router(db):
    router = APIRouter(prefix="/api/reminders")
    engine = ReminderEngine(db)

    @router.get("/status")
    def status():
        return engine.status()

    @router.post("/claim")
    def claim():
        return engine.claim()

    @router.post("/snooze")
    def snooze():
        return engine.snooze()

    return router
