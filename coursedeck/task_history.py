"""Local edit receipts and explicit, version-checked undo for the regular UI."""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .task_edits import TaskEdits


class UndoEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str = Field(min_length=1, max_length=64)
    expected_version: str = Field(min_length=64, max_length=64)


def build_task_history_router(db):
    router = APIRouter(prefix="/api/task-edits")
    edits = TaskEdits(db)

    @router.get("/{task_id:path}")
    def history(task_id: str):
        try:
            with db.connection() as conn:
                conn.execute("BEGIN")
                _, version = edits._read(conn, task_id)
                rows = conn.execute(
                    "SELECT * FROM task_edit_history WHERE task_id=? ORDER BY rowid DESC LIMIT 20",
                    (task_id,),
                ).fetchall()
                changes = []
                for row in rows:
                    before, after = (
                        json.loads(row["before_payload"]),
                        json.loads(row["after_payload"]),
                    )
                    changes.append(
                        {
                            "id": row["id"],
                            "created_at": row["created_at"],
                            "undone": bool(row["undone"]),
                            "fields": [key for key in after if before.get(key) != after[key]],
                        }
                    )
                return {"version": version, "changes": changes}
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc

    @router.post("/{task_id:path}/undo")
    def undo(task_id: str, value: UndoEdit):
        try:
            return edits.undo(task_id, value.change_id, value.expected_version)
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
