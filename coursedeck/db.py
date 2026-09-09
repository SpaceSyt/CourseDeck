import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .domain import LocalState, Outcome, Settings, SyncResult, now


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 6:
                raise RuntimeError("Database belongs to a newer CourseDeck version")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS courses (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    external_id TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(provider, external_id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    course_id TEXT NOT NULL REFERENCES courses(id),
                    external_id TEXT NOT NULL, payload TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    missing_count INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(provider, course_id, external_id)
                );
                CREATE TABLE IF NOT EXISTS task_local_states (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connector_states (
                    provider TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
                    attempted_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    outcome TEXT NOT NULL, task_count INTEGER NOT NULL,
                    warnings TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspace_courses (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
                    remote_course_id TEXT UNIQUE REFERENCES courses(id)
                );
                CREATE TABLE IF NOT EXISTS mail_messages (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    received_at TEXT NOT NULL, local_payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS mail_rules (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS custom_tasks (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    email_id TEXT REFERENCES mail_messages(id),
                    local_payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS course_links (
                    workspace_id TEXT NOT NULL REFERENCES workspace_courses(id) ON DELETE CASCADE,
                    remote_course_id TEXT PRIMARY KEY REFERENCES courses(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS course_preferences (
                    course_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)
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

    def apply(self, provider: str, result: SyncResult, attempted_at: str):
        stamp = now()
        seen = stamp.isoformat()
        valid = result.outcome in (Outcome.SUCCESS, Outcome.PARTIAL)
        if any(x.provider != provider for x in [*result.courses, *result.tasks]):
            raise ValueError("Connector crossed provider identity boundary")
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
                    unavailable = task.raw_data.get("unavailable_fields", [])
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
                if result.outcome == Outcome.SUCCESS and result.complete:
                    # Three FULL successful snapshots and at least seven days unseen.
                    db.execute(
                        """UPDATE tasks SET missing_count=missing_count+1
                        WHERE provider=? AND last_seen_at != ?""",
                        (provider, seen),
                    )
                    db.execute(
                        """UPDATE tasks SET archived=1 WHERE provider=?
                        AND missing_count>=3 AND last_seen_at<?""",
                        (provider, (stamp - timedelta(days=7)).isoformat()),
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

    def _patch_course_preferences(self, db, key, patch):
        row = db.execute(
            "SELECT payload FROM course_preferences WHERE course_id=?", (key,)
        ).fetchone()
        value = (json.loads(row[0]) if row else {}) | patch
        if "alias" in value:
            value["alias"] = (value["alias"] or "").strip() or None
        db.execute(
            "INSERT OR REPLACE INTO course_preferences VALUES (?, ?)", (key, json.dumps(value))
        )

    def source_courses(self):
        with self.connection() as db:
            return [
                json.loads(row["payload"]) | {"id": row["id"]}
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
        self, course_id: str, name: str, source_course_ids: list[str], alias=...
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
            db.execute("DELETE FROM course_links WHERE workspace_id=?", (key,))
            db.executemany("INSERT INTO course_links VALUES (?, ?)", [(key, item) for item in ids])
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
            rows = db.execute("""SELECT t.*, l.payload AS local_payload FROM tasks t
                LEFT JOIN task_local_states l ON l.task_id=t.id ORDER BY t.id""").fetchall()
        from .mail import custom_tasks

        course_map = {
            remote: course["id"]
            for course in self.courses()
            for remote in course["source_course_ids"]
        }

        return [
            json.loads(r["payload"])
            | {
                "id": r["id"],
                "course_id": course_map.get(r["course_id"], r["course_id"]),
                "source_course_id": r["course_id"],
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
                "archived": bool(r["archived"]),
                "missing_count": r["missing_count"],
                "local": json.loads(r["local_payload"])
                if r["local_payload"]
                else LocalState().model_dump(),
            }
            for r in rows
        ] + custom_tasks(self)

    def patch_local(self, task_id: str, patch: dict):
        with self.connection() as db:
            if task_id.startswith("custom:"):
                row = db.execute(
                    "SELECT local_payload FROM custom_tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                value = LocalState.model_validate(json.loads(row[0]) | patch)
                db.execute(
                    "UPDATE custom_tasks SET local_payload=? WHERE id=?",
                    (value.model_dump_json(), task_id),
                )
                return value
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError(task_id)
            row = db.execute(
                "SELECT payload FROM task_local_states WHERE task_id=?", (task_id,)
            ).fetchone()
            value = LocalState.model_validate((json.loads(row[0]) if row else {}) | patch)
            db.execute(
                "INSERT OR REPLACE INTO task_local_states VALUES (?, ?)",
                (task_id, value.model_dump_json()),
            )
        return value

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
        with self.connection() as source, sqlite3.connect(destination) as target:
            source.backup(target)
