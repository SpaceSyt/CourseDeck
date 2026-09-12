import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from .domain import LocalState, Outcome, Settings, SyncResult, now
from .migrations import initialize_component, migrate, validate_existing
from .task_state import project_task_state, unread_fields


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            validate_existing(db, "tasks", allow_historical=True)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 7:
                raise RuntimeError("Database belongs to a newer CourseDeck version")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            initialize_component(db, "tasks_core")
            if version < 3:
                # Retire the old demo source, including its independent local
                # edits and aliases. Other providers are never touched.
                db.execute("DELETE FROM workspace_courses WHERE provider='demo'")
                db.execute(
                    "DELETE FROM task_local_states WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE provider='demo')"
                )
                db.execute("DELETE FROM tasks WHERE provider='demo'")
                db.execute("DELETE FROM courses WHERE provider='demo'")
                db.execute("DELETE FROM connector_states WHERE provider='demo'")
                db.execute("DELETE FROM sync_history WHERE provider='demo'")
                db.execute("PRAGMA user_version=3")
            if version < 4:
                db.execute("PRAGMA user_version=4")
            if version < 5:
                db.execute("""INSERT OR IGNORE INTO course_links
                    SELECT id, remote_course_id FROM workspace_courses
                    WHERE remote_course_id IS NOT NULL""")
                db.execute("PRAGMA user_version=5")
            if version < 6:
                for entry in db.execute(
                    "SELECT w.*, c.payload FROM workspace_courses w "
                    "LEFT JOIN courses c ON c.id=w.remote_course_id"
                ).fetchall():
                    original = (
                        json.loads(entry["payload"])["name"] if entry["payload"] else entry["name"]
                    )
                    alias = entry["name"] if entry["name"] != original else None
                    db.execute(
                        "INSERT OR IGNORE INTO course_preferences VALUES (?, ?)",
                        (entry["id"], json.dumps({"alias": alias})),
                    )
                db.execute("PRAGMA user_version=6")
            if version < 7:
                # Missing from a source is not completion. Keep every cached task visible.
                db.execute("UPDATE tasks SET archived=0")
                for entry in db.execute("""SELECT provider, finished_at FROM sync_history
                    WHERE id IN (SELECT MAX(id) FROM sync_history GROUP BY provider)""").fetchall():
                    self._record_finished_sync(db, entry["provider"], entry["finished_at"])
                db.execute("PRAGMA user_version=7")
            from .changes import initialize

            initialize(db)
            migrate(db, "tasks")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self) -> Settings:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM settings WHERE id=1").fetchone()
        return Settings.model_validate_json(row[0]) if row else Settings()

    def save_settings(self, settings: Settings):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (1, ?)", (settings.model_dump_json(),)
            )

    def state(self, provider: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM connector_states WHERE provider=?", (provider,)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def update_state(self, provider: str, **values):
        state = self.state(provider) | values
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO connector_states VALUES (?, ?)",
                (provider, json.dumps(state)),
            )

    def _record_finished_sync(self, db, provider: str, finished_at: str):
        row = db.execute(
            "SELECT payload FROM connector_states WHERE provider=?", (provider,)
        ).fetchone()
        state = (json.loads(row[0]) if row else {}) | {"last_finished_sync": finished_at}
        db.execute(
            "INSERT OR REPLACE INTO connector_states VALUES (?, ?)",
            (provider, json.dumps(state)),
        )

    def apply(self, provider: str, result: SyncResult, attempted_at: str):
        stamp = now()
        seen = stamp.isoformat()
        valid = result.outcome in (Outcome.SUCCESS, Outcome.PARTIAL)
        if any(x.provider != provider for x in [*result.courses, *result.tasks]):
            raise ValueError("Connector crossed provider identity boundary")
        course_ids = {course.external_id for course in result.courses}
        if any(scope.course_external_id not in course_ids for scope in result.covered_task_scopes):
            raise ValueError("Covered task scope belongs to an unread course")
        # Everything, including history, commits atomically. Failed syncs never touch tasks.
        with self.connection() as db:
            if valid:
                for course in result.courses:
                    db.execute(
                        """INSERT INTO courses VALUES (?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                        (course.id, provider, course.external_id, course.model_dump_json()),
                    )
                for task in result.tasks:
                    payload = task.model_dump(mode="json")
                    unavailable = unread_fields(task.raw_data)
                    if unavailable:
                        previous = db.execute(
                            "SELECT payload FROM tasks WHERE id=?", (task.id,)
                        ).fetchone()
                        if previous:
                            old = json.loads(previous[0])
                            for field in unavailable:
                                if field in {
                                    "due_at",
                                    "closes_at",
                                    "available_at",
                                    "description",
                                    "submission_status",
                                    "score",
                                    "points_possible",
                                    "graded",
                                }:
                                    payload[field] = old.get(field)
                    db.execute(
                        """INSERT INTO tasks
                        (id, provider, course_id, external_id, payload, first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                        last_seen_at=excluded.last_seen_at, missing_count=0, archived=0""",
                        (
                            task.id,
                            provider,
                            task.course_id,
                            task.external_id,
                            json.dumps(payload),
                            seen,
                            seen,
                        ),
                    )
                full_snapshot = result.outcome == Outcome.SUCCESS and result.complete
                if full_snapshot or result.covered_task_scopes:
                    scopes = {}
                    for scope in result.covered_task_scopes:
                        scopes.setdefault(scope.course_external_id, []).append(
                            scope.external_id_prefix
                        )
                    unseen = db.execute(
                        """SELECT t.id, t.external_id, c.external_id AS course_external_id
                        FROM tasks t JOIN courses c ON c.id=t.course_id
                        WHERE t.provider=? AND t.last_seen_at != ?""",
                        (provider, seen),
                    ).fetchall()
                    missing_ids = [
                        (row["id"],)
                        for row in unseen
                        if full_snapshot
                        or any(
                            row["external_id"].startswith(prefix)
                            for prefix in scopes.get(row["course_external_id"], [])
                        )
                    ]
                    db.executemany(
                        "UPDATE tasks SET missing_count=missing_count+1 WHERE id=?", missing_ids
                    )
            db.execute(
                """INSERT INTO sync_history
                (provider, attempted_at, finished_at, outcome, task_count, warnings, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider,
                    attempted_at,
                    seen,
                    result.outcome,
                    len(result.tasks) if valid else 0,
                    json.dumps(result.warnings),
                    json.dumps(result.metadata),
                ),
            )
            db.execute(
                "DELETE FROM sync_history WHERE id NOT IN "
                "(SELECT id FROM sync_history ORDER BY id DESC LIMIT 1000)"
            )
            # Retained independently of history pruning, atomically with the snapshot.
            self._record_finished_sync(db, provider, seen)
            from .changes import record_sync

            record_sync(db, provider, seen, result.outcome)

    def courses(self):
        with self.connection() as db:
            sources = {
                row["id"]: json.loads(row["payload"]) | {"id": row["id"]}
                for row in db.execute("SELECT * FROM courses ORDER BY id")
            }
            local = db.execute("SELECT * FROM workspace_courses ORDER BY name").fetchall()
            links = db.execute("SELECT * FROM course_links ORDER BY rowid").fetchall()
            preferences = {
                row["course_id"]: json.loads(row["payload"])
                for row in db.execute("SELECT * FROM course_preferences")
            }
        result = dict(sources)
        for entry in local:
            remote_ids = [r["remote_course_id"] for r in links if r["workspace_id"] == entry["id"]]
            key = entry["remote_course_id"] or entry["id"]
            members = [sources[key] for key in remote_ids if key in sources]
            for remote_id in remote_ids:
                result.pop(remote_id, None)
            result[key] = sources.get(
                key,
                {
                    "id": key,
                    "provider": entry["provider"],
                    "external_id": None,
                    "section": None,
                    "teacher": None,
                    "source_url": None,
                },
            ) | {
                "name": entry["name"],
                "source_name": sources.get(key, {}).get("name"),
                "source_names": list(dict.fromkeys(c["name"] for c in members)),
                "source_course_ids": remote_ids,
                "providers": list(dict.fromkeys(c["provider"] for c in members))
                or [entry["provider"]],
                "workspace_id": entry["id"],
                "needs_binding": not remote_ids,
            }
        for course in result.values():
            course.setdefault("source_course_ids", [course["id"]])
            course.setdefault("source_names", [course["name"]])
            course.setdefault("providers", [course["provider"]])
            original = course.get("source_name") or course["name"]
            local = preferences.get(course.get("workspace_id") or course["id"], {})
            alias = local.get("alias", course["name"] if course["name"] != original else None)
            course.update(
                original_name=original,
                alias=alias,
                name=alias or original,
                disabled=local.get("disabled", False),
                deleted=local.get("deleted", False),
                color=local.get("color"),
            )
            course["source_names"] = list(
                dict.fromkeys([*course["source_names"], *local.get("merged_names", [])])
            )
        return sorted(result.values(), key=lambda c: c["name"].casefold())

    def patch_course(self, course_id: str, patch: dict):
        course = next(
            (c for c in self.courses() if course_id in (c["id"], c.get("workspace_id"))), None
        )
        if course is None:
            raise KeyError(course_id)
        key = course.get("workspace_id") or course["id"]
        with self.connection() as db:
            self._patch_course_preferences(db, key, patch)

    def resolve_course_alias(self, course_id: str | None) -> str | None:
        if course_id is None:
            return None
        courses = self.courses()
        # Existing source membership wins if a previously merged source was later detached.
        for course in courses:
            if course_id in (
                course["id"],
                course.get("workspace_id"),
                *course["source_course_ids"],
            ):
                return course["id"]
        with self.connection() as db:
            preferences = {
                row["course_id"]: json.loads(row["payload"])
                for row in db.execute("SELECT course_id, payload FROM course_preferences")
            }
        for course in courses:
            saved = preferences.get(course.get("workspace_id") or course["id"], {})
            pending = list(saved.get("merged_courses", []))
            while pending:
                previous = pending.pop()
                if course_id in (previous.get("id"), *previous.get("source_course_ids", [])):
                    return course["id"]
                pending.extend(previous.get("preferences", {}).get("merged_courses", []))
        return None

    def _patch_course_preferences(self, db, key, patch):
        row = db.execute(
            "SELECT payload FROM course_preferences WHERE course_id=?", (key,)
        ).fetchone()
        value = (json.loads(row[0]) if row else {}) | patch
        if "alias" in value:
            value["alias"] = (value["alias"] or "").strip() or None
        if "color" in patch:
            color = patch["color"]
            if color is not None and (
                not isinstance(color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None
            ):
                raise ValueError("Color must be a six-digit hex color or null")
            value["color"] = color.lower() if color is not None else None
        db.execute(
            "INSERT OR REPLACE INTO course_preferences VALUES (?, ?)", (key, json.dumps(value))
        )

    def source_courses(self):
        owners = {
            source_id: course
            for course in self.courses()
            for source_id in course["source_course_ids"]
        }
        with self.connection() as db:
            return [
                json.loads(row["payload"])
                | {
                    "id": row["id"],
                    "workspace_id": owners.get(row["id"], {}).get("workspace_id"),
                    "color": owners.get(row["id"], {}).get("color"),
                }
                for row in db.execute("SELECT * FROM courses ORDER BY id")
            ]

    def _validate_binding(self, db, provider: str, remote_id: str | None, workspace_id=None):
        if remote_id is None:
            return
        row = db.execute("SELECT provider FROM courses WHERE id=?", (remote_id,)).fetchone()
        if not row or row["provider"] != provider:
            raise ValueError("Select a synced course from the chosen source")
        existing = db.execute(
            "SELECT workspace_id AS id FROM course_links WHERE remote_course_id=?", (remote_id,)
        ).fetchone()
        if existing and existing["id"] != workspace_id:
            raise ValueError("This source course is already linked")

    def add_course(
        self,
        name: str,
        provider: str,
        remote_course_id: str | None,
        source_course_ids: list[str] | None = None,
        color=None,
    ):
        key = "local:" + uuid4().hex
        with self.connection() as db:
            ids = (
                list(dict.fromkeys(source_course_ids))
                if source_course_ids is not None
                else ([remote_course_id] if remote_course_id else [])
            )
            if source_course_ids is None:
                self._validate_binding(db, provider, remote_course_id)
            self._validate_links(db, ids)
            primary = ids[0] if ids else None
            if primary:
                provider = db.execute(
                    "SELECT provider FROM courses WHERE id=?", (primary,)
                ).fetchone()[0]
            db.execute(
                "INSERT INTO workspace_courses VALUES (?, ?, ?, ?)",
                (key, name, provider, primary),
            )
            db.executemany("INSERT INTO course_links VALUES (?, ?)", [(key, item) for item in ids])
            self._save_course_alias(db, key, name, primary)
            self._patch_course_preferences(db, key, {"color": color})
        return key

    def _save_course_alias(self, db, key, name, primary):
        row = db.execute("SELECT payload FROM courses WHERE id=?", (primary,)).fetchone()
        original = json.loads(row[0])["name"] if row else name
        self._patch_course_preferences(db, key, {"alias": name if name != original else None})

    def _validate_links(self, db, ids, workspace_id=None):
        for remote_id in ids:
            if not db.execute("SELECT 1 FROM courses WHERE id=?", (remote_id,)).fetchone():
                raise ValueError("Source course not found")
            owner = db.execute(
                "SELECT workspace_id FROM course_links WHERE remote_course_id=?", (remote_id,)
            ).fetchone()
            if owner and owner[0] != workspace_id:
                raise ValueError("This source course is already linked to another course")

    def update_course_sources(
        self, course_id: str, name: str, source_course_ids: list[str], alias=..., color=...
    ):
        ids = list(dict.fromkeys(source_course_ids))
        with self.connection() as db:
            entry = db.execute(
                "SELECT * FROM workspace_courses WHERE id=? OR remote_course_id=?",
                (course_id, course_id),
            ).fetchone()
            if entry:
                key, provider = entry["id"], entry["provider"]
            else:
                remote = db.execute(
                    "SELECT provider FROM courses WHERE id=?", (course_id,)
                ).fetchone()
                if not remote:
                    raise KeyError(course_id)
                # A source course already in a group must be edited through its group.
                if db.execute(
                    "SELECT 1 FROM course_links WHERE remote_course_id=?", (course_id,)
                ).fetchone():
                    raise ValueError("Edit the linked course instead")
                key, provider = "local:" + uuid4().hex, remote["provider"]
            self._validate_links(db, ids, key)
            primary = (
                entry["remote_course_id"]
                if entry and entry["remote_course_id"] in ids
                else (ids[0] if ids else None)
            )
            if primary:
                provider = db.execute(
                    "SELECT provider FROM courses WHERE id=?", (primary,)
                ).fetchone()[0]
            if entry:
                db.execute(
                    "UPDATE workspace_courses SET name=?, provider=?, remote_course_id=? "
                    "WHERE id=?",
                    (name, provider, primary, key),
                )
            else:
                db.execute(
                    "INSERT INTO workspace_courses VALUES (?, ?, ?, ?)",
                    (key, name, provider, primary),
                )
                # A raw source course becomes a local group without losing its local settings.
                db.execute(
                    "UPDATE course_preferences SET course_id=? WHERE course_id=?",
                    (key, course_id),
                )
            if alias is not ...:
                self._patch_course_preferences(db, key, {"alias": alias})
            else:
                self._save_course_alias(db, key, name, primary)
            if color is not ...:
                self._patch_course_preferences(db, key, {"color": color})
            db.execute("DELETE FROM course_links WHERE workspace_id=?", (key,))
            db.executemany("INSERT INTO course_links VALUES (?, ?)", [(key, item) for item in ids])
        return key

    def _merge_course_record(self, db, course_id):
        row = db.execute(
            "SELECT * FROM workspace_courses WHERE id=? OR remote_course_id=?",
            (course_id, course_id),
        ).fetchone()
        if row:
            record = dict(row)
            record["source_course_ids"] = [
                item[0]
                for item in db.execute(
                    "SELECT remote_course_id FROM course_links WHERE workspace_id=? ORDER BY rowid",
                    (row["id"],),
                )
            ]
            record["workspace_id"] = row["id"]
        else:
            row = db.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
            if row is None:
                raise KeyError(course_id)
            if db.execute(
                "SELECT 1 FROM course_links WHERE remote_course_id=?", (course_id,)
            ).fetchone():
                raise ValueError("Select the linked course to merge all of its sources")
            record = {
                "id": course_id,
                "name": json.loads(row["payload"])["name"],
                "provider": row["provider"],
                "remote_course_id": course_id,
                "source_course_ids": [course_id],
                "workspace_id": None,
            }
        preferences = db.execute(
            "SELECT payload FROM course_preferences WHERE course_id=?", (record["id"],)
        ).fetchone()
        record["preferences"] = json.loads(preferences[0]) if preferences else {}
        return record

    def merge_courses(self, target_id: str, course_ids: list[str]):
        if not course_ids:
            raise ValueError("Select a course to merge")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._merge_course_record(db, target_id)
            sources = {}
            for course_id in course_ids:
                source = self._merge_course_record(db, course_id)
                if source["id"] == target["id"]:
                    raise ValueError("Choose a different destination course")
                sources[source["id"]] = source
            if target["preferences"].get("deleted") or any(
                source["preferences"].get("deleted") for source in sources.values()
            ):
                raise ValueError("Restore deleted courses before merging")
            key = target["workspace_id"] or "local:" + uuid4().hex
            if not target["workspace_id"]:
                db.execute(
                    "INSERT INTO workspace_courses VALUES (?, ?, ?, ?)",
                    (key, target["name"], target["provider"], target["remote_course_id"]),
                )
                db.execute(
                    "UPDATE course_preferences SET course_id=? WHERE course_id=?",
                    (key, target["id"]),
                )
            members = list(
                dict.fromkeys(
                    [
                        *target["source_course_ids"],
                        *(
                            member
                            for source in sources.values()
                            for member in source["source_course_ids"]
                        ),
                    ]
                )
            )
            references = {target["id"], *members, *sources}
            merged = list(target["preferences"].get("merged_courses", []))
            merged_names = list(target["preferences"].get("merged_names", []))
            for source in sources.values():
                merged.append(
                    {
                        "id": source["id"],
                        "name": source["name"],
                        "source_course_ids": source["source_course_ids"],
                        "preferences": source["preferences"],
                    }
                )
                merged_names.extend(
                    [
                        source["preferences"].get("alias") or source["name"],
                        *source["preferences"].get("merged_names", []),
                    ]
                )
                if source["workspace_id"]:
                    db.execute(
                        "UPDATE course_links SET workspace_id=? WHERE workspace_id=?",
                        (key, source["workspace_id"]),
                    )
                    db.execute(
                        "DELETE FROM workspace_courses WHERE id=?", (source["workspace_id"],)
                    )
                    db.execute("DELETE FROM course_preferences WHERE course_id=?", (source["id"],))
            primary = target["remote_course_id"] or (members[0] if members else None)
            provider = (
                db.execute("SELECT provider FROM courses WHERE id=?", (primary,)).fetchone()[0]
                if primary
                else target["provider"]
            )
            db.execute(
                "UPDATE workspace_courses SET provider=?, remote_course_id=? WHERE id=?",
                (provider, primary, key),
            )
            db.executemany(
                "INSERT OR IGNORE INTO course_links VALUES (?, ?)",
                [(key, member) for member in members],
            )
            self._patch_course_preferences(
                db,
                key,
                {
                    "merged_courses": merged,
                    "merged_names": list(dict.fromkeys(merged_names)),
                },
            )
            if (
                not target["source_course_ids"]
                and primary
                and not target["preferences"].get("alias")
            ):
                self._save_course_alias(db, key, target["name"], primary)
            for table, column in [("custom_tasks", "payload"), ("mail_messages", "local_payload")]:
                for row in db.execute(f"SELECT id, {column} FROM {table}").fetchall():
                    payload = json.loads(row[column])
                    if payload.get("course_id") in references:
                        payload["course_id"] = key
                        db.execute(
                            f"UPDATE {table} SET {column}=? WHERE id=?",
                            (json.dumps(payload), row["id"]),
                        )
            for row in db.execute("SELECT id, payload FROM mail_rules").fetchall():
                payload = json.loads(row["payload"])
                if payload.get("action") == "course" and payload.get("value") in references:
                    payload["value"] = key
                    db.execute(
                        "UPDATE mail_rules SET payload=? WHERE id=?",
                        (json.dumps(payload), row["id"]),
                    )
        return key

    def bind_course(self, workspace_id: str, remote_course_id: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM workspace_courses WHERE id=?", (workspace_id,)
            ).fetchone()
            if not row:
                raise KeyError(workspace_id)
            self._validate_links(db, [remote_course_id], workspace_id)
            db.execute(
                "INSERT OR IGNORE INTO course_links VALUES (?, ?)", (workspace_id, remote_course_id)
            )
            db.execute(
                "UPDATE workspace_courses SET remote_course_id=COALESCE(remote_course_id, ?) "
                "WHERE id=?",
                (remote_course_id, workspace_id),
            )
            if not row["remote_course_id"]:
                self._save_course_alias(db, workspace_id, row["name"], remote_course_id)

    def tasks(self):
        with self.connection() as db:
            rows = db.execute(
                """SELECT t.*, l.payload AS local_payload FROM tasks t
                LEFT JOIN task_local_states l ON l.task_id=t.id
                ORDER BY t.id"""
            ).fetchall()
            checked_at = {
                row["provider"]: json.loads(row["payload"]).get("last_finished_sync")
                for row in db.execute("SELECT provider, payload FROM connector_states")
                if "last_finished_sync" in json.loads(row["payload"])
            }
        from .mail import custom_tasks

        course_map = {
            remote: course["id"]
            for course in self.courses()
            for remote in course["source_course_ids"]
        }

        tasks = [
            (payload := json.loads(r["payload"]))
            | {
                "id": r["id"],
                "course_id": course_map.get(r["course_id"], r["course_id"]),
                "source_course_id": r["course_id"],
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
                "archived": bool(r["archived"]),
                "missing_count": r["missing_count"],
                "source_availability": "missing"
                if r["missing_count"]
                else (
                    "present"
                    if r["last_seen_at"] == checked_at.get(r["provider"], r["last_seen_at"])
                    else "unconfirmed"
                ),
                "source_status_known": bool(payload.get("submission_status"))
                and payload["submission_status"] != "unknown"
                and r["last_seen_at"] == checked_at.get(r["provider"], r["last_seen_at"])
                and "submission_status"
                not in payload.get("raw_data", {}).get("unavailable_fields", []),
                "local": json.loads(r["local_payload"])
                if r["local_payload"]
                else LocalState().model_dump(),
            }
            for r in rows
        ] + custom_tasks(self)
        for task in tasks:
            task["source_due_at"] = task.get("due_at")
            task.update(project_task_state(task))
            if task["local"].get("due_override"):
                task["due_at"] = task["local"].get("due_at_override")
        return tasks

    def patch_local(self, task_id: str, patch: dict):
        from .task_edits import TaskEdits

        result = TaskEdits(self).patch_local(task_id, patch)
        return LocalState.model_validate(result["after"])

    def history(self, limit=100):
        with self.connection() as db:
            return [
                dict(row)
                | {"warnings": json.loads(row["warnings"]), "metadata": json.loads(row["metadata"])}
                for row in db.execute(
                    "SELECT * FROM sync_history ORDER BY id DESC LIMIT ?", (limit,)
                )
            ]

    def backup(self, destination: Path):
        with self.connection() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
