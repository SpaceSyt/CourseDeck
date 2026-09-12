"""Registered SQLite schema upgrades. Never migrate an unrecognized database.

The original task user_version (1..7) remains owned by its historical data
migrations. These component versions govern all subsequent schema changes.
"""

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path


class MigrationError(ValueError):
    pass


COMPONENTS = {
    "tasks": ("tasks_core", "changes", "associations", "task_edits"),
    "knowledge": ("knowledge_core", "material_checkpoints"),
}
REGISTRY_SQL = """CREATE TABLE IF NOT EXISTS schema_migrations (
    component TEXT PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL
)"""
LEGACY_COLUMNS = {
    "documents": {"metadata": "TEXT NOT NULL DEFAULT '{}'"},
    "messages": {"warnings": "TEXT NOT NULL DEFAULT '[]'"},
    "conversations": {"task_id": "TEXT"},
}


def _rows(connection, sql):
    return tuple(tuple(row) for row in connection.execute(sql))


def structure(connection):
    """Compare SQLite semantics, independent of CREATE vs ALTER formatting."""
    result = {}
    for kind, name, table, sql in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type,name"
    ):
        if not re.fullmatch(r"[a-z_]+", name):
            raise MigrationError("Unknown database schema object.")
        if kind == "table":
            # None of the supported layouts has hidden/generated columns or CHECKs.
            if re.search(r"\b(CHECK|GENERATED|COLLATE|STRICT)\b|WITHOUT\s+ROWID", sql, re.I):
                raise MigrationError("Unknown database schema constraints.")
            indices = []
            for row in connection.execute(f'PRAGMA index_list("{name}")'):
                indices.append(
                    (row[2], row[3], row[4], _rows(connection, f'PRAGMA index_info("{row[1]}")'))
                )
            result[name] = (
                kind,
                _rows(connection, f'PRAGMA table_info("{name}")'),
                _rows(connection, f'PRAGMA foreign_key_list("{name}")'),
                tuple(sorted(indices, key=repr)),
            )
        elif kind == "index":
            result[name] = (kind, table, re.sub(r"\s+", "", sql).casefold())
        else:
            raise MigrationError("Unknown database schema object type.")
    return result


def _execute_schema(connection, script):
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def initialize_component(connection, component):
    """Run one registered DDL block without implicitly committing its caller."""
    _execute_schema(connection, SCHEMAS[component])


@lru_cache(maxsize=4)
def _reference(kind):
    if kind not in COMPONENTS:
        raise MigrationError("Unknown database kind.")
    with closing(sqlite3.connect(":memory:")) as reference:
        members = {}
        for component in COMPONENTS[kind]:
            before = set(structure(reference))
            _execute_schema(reference, SCHEMAS[component])
            members[component] = set(structure(reference)) - before
        reference.execute(REGISTRY_SQL)
        return structure(reference), members


def versions(connection):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return {}
    return {
        row[0]: row[1]
        for row in connection.execute("SELECT component,version FROM schema_migrations")
    }


def validate_existing(connection, kind, *, allow_historical=False):
    expected, members = _reference(kind)
    actual = structure(connection)
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version > (7 if kind == "tasks" else 0):
        raise MigrationError("Database belongs to a newer CourseDeck version.")
    if not actual:
        if user_version:
            raise MigrationError("Database schema is incomplete.")
        return {}
    unknown = set(actual) - set(expected)
    if unknown:
        raise MigrationError("Unknown database schema; migration is unsupported.")
    if (
        "schema_migrations" in actual
        and actual["schema_migrations"] != expected["schema_migrations"]
    ):
        raise MigrationError("Unknown database migration registry schema.")
    current_versions = versions(connection)
    if any(
        name not in COMPONENTS[kind] or type(version) is not int or version != 1
        for name, version in current_versions.items()
    ):
        raise MigrationError("Unknown or newer database migration version.")
    for name, value in actual.items():
        target = expected[name]
        if value == target:
            continue
        if kind == "knowledge" and name in LEGACY_COLUMNS and not current_versions:
            # Only these explicitly released additive column changes are known.
            absent = set(LEGACY_COLUMNS[name]) - {row[1] for row in value[1]}
            columns = tuple(row for row in target[1] if row[1] not in absent)
            columns = tuple((index, *row[1:]) for index, row in enumerate(columns))
            if value == (target[0], columns, target[2], target[3]):
                continue
        raise MigrationError("Unknown database schema; migration is unsupported.")
    core = "tasks_core" if kind == "tasks" else "knowledge_core"
    required = set(members[core])
    if kind == "knowledge" and not current_versions:
        required.discard("chat_activity")
    if kind == "tasks" and allow_historical and user_version < 7:
        required = {
            "courses",
            "tasks",
            "task_local_states",
            "connector_states",
            "sync_history",
            "settings",
        }
        if user_version >= 3:
            required.add("workspace_courses")
        if user_version >= 4:
            required.update({"mail_messages", "mail_rules", "custom_tasks"})
        if user_version >= 5:
            required.add("course_links")
        if user_version >= 6:
            required.add("course_preferences")
    if not required <= set(actual):
        raise MigrationError("Database schema is incomplete; migration is unsupported.")
    for component, names in members.items():
        present = names & set(actual)
        if component in current_versions and present != names:
            raise MigrationError("Versioned database schema is incomplete.")
        if component != core and present and present != names:
            raise MigrationError("Database schema component is incomplete.")
    if kind == "tasks" and user_version != 7 and not allow_historical:
        raise MigrationError("This historical task database requires its original data migration.")
    return current_versions


def migrate(connection, kind):
    """Apply registered additive upgrades atomically; caller owns the transaction."""
    previous = validate_existing(connection, kind)
    before = structure(connection)
    connection.execute("SAVEPOINT schema_upgrade")
    try:
        if kind == "knowledge" and before:
            for table, fields in LEGACY_COLUMNS.items():
                existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                for column, definition in fields.items():
                    if column not in existing:
                        connection.execute(
                            f'ALTER TABLE "{table}" ADD COLUMN {column} {definition}'
                        )
        for component in COMPONENTS[kind]:
            _execute_schema(connection, SCHEMAS[component])
        connection.execute(REGISTRY_SQL)
        stamp = datetime.now(UTC).isoformat()
        for component in COMPONENTS[kind]:
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?,1,?)", (component, stamp)
            )
        if kind == "tasks" and not before:
            connection.execute("PRAGMA user_version=7")
        validate_existing(connection, kind)
        if structure(connection) != _reference(kind)[0]:
            raise MigrationError("Migrated database schema did not match the registered target.")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationError("Database contains broken references.")
        connection.execute("RELEASE schema_upgrade")
    except Exception:
        connection.execute("ROLLBACK TO schema_upgrade")
        connection.execute("RELEASE schema_upgrade")
        raise
    return {"from": previous, "to": versions(connection)}


def migrate_copy(path, kind):
    """Only call on a private disposable backup copy, never the selected original."""
    if kind == "tasks":
        # Reuse the historical data migrations: aliases, course bindings, cache
        # visibility and last-finished sync metadata cannot be upgraded by DDL alone.
        from .db import Database

        with closing(sqlite3.connect(path)) as connection:
            previous = versions(connection)
            old_version = connection.execute("PRAGMA user_version").fetchone()[0]
        upgraded = Database(Path(path))
        with upgraded.connection() as connection:
            return {"from": previous, "to": versions(connection), "from_user_version": old_version}
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            result = migrate(connection, kind)
        return result


SCHEMAS = {
    "tasks_core": """
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
                )
    """,
    "changes": """
CREATE TABLE IF NOT EXISTS task_change_baselines (
            task_id TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
CREATE TABLE IF NOT EXISTS task_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            provider TEXT NOT NULL, course_id TEXT NOT NULL, title TEXT NOT NULL,
            kind TEXT NOT NULL, severity TEXT NOT NULL, before_value TEXT,
            after_value TEXT, evidence TEXT NOT NULL, observed_at TEXT NOT NULL,
            read_at TEXT
        );
CREATE INDEX IF NOT EXISTS changes_course ON task_changes(course_id,id);
CREATE INDEX IF NOT EXISTS changes_unread ON task_changes(read_at,severity);
CREATE TABLE IF NOT EXISTS change_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """,
    "associations": """
CREATE TABLE IF NOT EXISTS task_relations (
                    id TEXT PRIMARY KEY, left_kind TEXT NOT NULL, left_id TEXT NOT NULL,
                    right_kind TEXT NOT NULL, right_id TEXT NOT NULL,
                    state TEXT NOT NULL, active INTEGER NOT NULL,
                    evidence TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
CREATE TABLE IF NOT EXISTS task_relation_history (
                    id INTEGER PRIMARY KEY, relation_id TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL
                )
    """,
    "task_edits": """
CREATE TABLE IF NOT EXISTS task_edit_history (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, before_payload TEXT NOT NULL,
                after_payload TEXT NOT NULL, created_at TEXT NOT NULL,
                undone INTEGER NOT NULL DEFAULT 0
            )
    """,
    "knowledge_core": """
CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, course_id TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, url TEXT, kind TEXT NOT NULL,
                    updated_at TEXT NOT NULL, source_task_id TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
CREATE INDEX IF NOT EXISTS document_courses ON documents(course_id);
CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, course_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, task_id TEXT
                );
CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL,
                    created_at TEXT NOT NULL, warnings TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );
CREATE INDEX IF NOT EXISTS conversation_messages ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS chat_activity (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
CREATE TABLE IF NOT EXISTS configuration (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL
                )
    """,
    "material_checkpoints": """
CREATE TABLE IF NOT EXISTS material_checkpoints (
            provider TEXT NOT NULL, scope TEXT NOT NULL, next_identity TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (provider, scope)
        )
    """,
}
