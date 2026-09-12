"""Reversible local overrides; no edits to source task payloads."""

import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .domain import LocalState, now


class TaskEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=2048)
    expected_version: str = Field(min_length=64, max_length=64)
    due_at: AwareDatetime | None = None
    restore_due_date: bool = False
    completion: Literal["done", "open", "source"] | None = None
    note: str | None = Field(default=None, max_length=10000)


class TaskEdits:
    def __init__(self, db):
        self.db = db

    def _read(self, conn, task_id):
        source_state = {}
        if task_id.startswith("custom:"):
            row = conn.execute(
                "SELECT payload, local_payload FROM custom_tasks WHERE id=?", (task_id,)
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT t.payload, t.missing_count, t.archived, t.last_seen_at,
                s.payload AS connector_payload, COALESCE(l.payload, '{}') AS local_payload
                FROM tasks t LEFT JOIN task_local_states l ON l.task_id=t.id
                LEFT JOIN connector_states s ON s.provider=t.provider WHERE t.id=?""",
                (task_id,),
            ).fetchone()
        if not row:
            raise KeyError(task_id)
        payload = json.loads(row["payload"])
        local = LocalState.model_validate_json(row["local_payload"]).model_dump(mode="json")
        if not task_id.startswith("custom:"):
            connector = json.loads(row["connector_payload"] or "{}")
            source_state = {
                "missing_count": row["missing_count"],
                "archived": row["archived"],
                "current": row["last_seen_at"]
                == connector.get("last_finished_sync", row["last_seen_at"]),
            }
        version = hashlib.sha256(
            json.dumps([payload, local, source_state], sort_keys=True).encode()
        ).hexdigest()
        return local, version

    def version(self, task_id):
        with self.db.connection() as conn:
            return self._read(conn, task_id)[1]

    def recent(self, task_id):
        with self.db.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id AS change_id, created_at, undone FROM task_edit_history "
                    "WHERE task_id=? ORDER BY rowid DESC LIMIT 5",
                    (task_id,),
                )
            ]

    def _write(self, conn, task_id, local):
        body = LocalState.model_validate(local).model_dump_json()
        if task_id.startswith("custom:"):
            conn.execute("UPDATE custom_tasks SET local_payload=? WHERE id=?", (body, task_id))
        else:
            conn.execute("INSERT OR REPLACE INTO task_local_states VALUES (?, ?)", (task_id, body))

    def update(self, value: TaskEdit, *, guard=None):
        if value.restore_due_date and "due_at" in value.model_fields_set:
            raise ValueError("Choose a new deadline or restore source deadline")
        patch = {}
        if value.restore_due_date:
            patch.update(due_override=False, due_at_override=None)
        elif "due_at" in value.model_fields_set:
            patch.update(due_override=True, due_at_override=value.due_at)
        if value.completion:
            patch.update(
                completion_override=None if value.completion == "source" else value.completion,
                dismissed=False,
            )
        if value.note is not None:
            patch["note"] = value.note
        return self._apply(value.task_id, patch, value.expected_version, guard=guard)

    def patch_local(self, task_id, patch, expected_version=None):
        """UI and compatibility entry point; omitted versions still update atomically.

        Only explicitly supplied fields change, preserving unrelated concurrent edits.
        A checkbox action intentionally clears an earlier completion override.
        """
        unknown = set(patch) - set(LocalState.model_fields)
        if unknown:
            raise ValueError("Unsupported local task fields")
        patch = dict(patch)
        if "dismissed" in patch:
            patch["completion_override"] = None
        return self._apply(task_id, patch, expected_version)

    def _apply(self, task_id, patch, expected_version, *, guard=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if guard:
                guard()
            before, version = self._read(conn, task_id)
            if expected_version is not None and version != expected_version:
                raise ValueError("Task changed; read it again before editing")
            after = LocalState.model_validate(before | patch).model_dump(mode="json")
            if before == after:
                return {
                    "changed": False,
                    "task_id": task_id,
                    "version": version,
                    "before": before,
                    "after": after,
                }
            self._write(conn, task_id, after)
            change_id = uuid4().hex
            conn.execute(
                "INSERT INTO task_edit_history VALUES (?, ?, ?, ?, ?, 0)",
                (
                    change_id,
                    task_id,
                    json.dumps(before),
                    json.dumps(after),
                    now().isoformat(),
                ),
            )
            return {
                "changed": True,
                "task_id": task_id,
                "change_id": change_id,
                "before": before,
                "after": after,
                "version": self._read(conn, task_id)[1],
            }

    def undo(self, task_id, change_id, expected_version, *, guard=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if guard:
                guard()
            row = conn.execute(
                "SELECT * FROM task_edit_history WHERE id=? AND task_id=?", (change_id, task_id)
            ).fetchone()
            if not row:
                raise ValueError("Change not found")
            if row["undone"]:
                return {"changed": False, "task_id": task_id, "change_id": change_id}
            current, version = self._read(conn, task_id)
            if version != expected_version or current != json.loads(row["after_payload"]):
                raise ValueError("Task changed since this edit; inspect it before undoing")
            self._write(conn, task_id, json.loads(row["before_payload"]))
            conn.execute("UPDATE task_edit_history SET undone=1 WHERE id=?", (change_id,))
            return {
                "changed": True,
                "task_id": task_id,
                "undone": change_id,
                "version": self._read(conn, task_id)[1],
            }
