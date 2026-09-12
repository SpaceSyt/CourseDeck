import json
import sqlite3
from contextlib import closing

import pytest

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.migrations import MigrationError, migrate, structure, validate_existing, versions
from coursedeck.recovery import (
    RecoveryError,
    RecoveryManager,
    digest,
    inspect_database,
    write_manifest,
)


def stores(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    return db, knowledge, RecoveryManager(tmp_path, db, knowledge)


def update_manifest(manager, backup):
    folder = manager.folder(backup["id"])
    for name in manager.stores:
        path = folder / name
        backup["files"][name] = {
            "schema": inspect_database(path),
            "sha256": digest(path),
            "bytes": path.stat().st_size,
        }
    write_manifest(folder / "manifest.json", backup)


def test_new_databases_register_all_components_and_startup_is_idempotent(tmp_path):
    db, knowledge, _ = stores(tmp_path)
    with db.connection() as connection:
        assert versions(connection) == {
            "tasks_core": 1,
            "changes": 1,
            "associations": 1,
            "task_edits": 1,
        }
        task_history = connection.execute("SELECT * FROM schema_migrations").fetchall()
    with knowledge.connection() as connection:
        assert versions(connection) == {"knowledge_core": 1, "material_checkpoints": 1}
        knowledge_history = connection.execute("SELECT * FROM schema_migrations").fetchall()
    Database(db.path)
    KnowledgeStore(knowledge.path)
    with db.connection() as connection:
        assert connection.execute("SELECT * FROM schema_migrations").fetchall() == task_history
    with knowledge.connection() as connection:
        assert connection.execute("SELECT * FROM schema_migrations").fetchall() == knowledge_history


def test_known_old_pair_upgrades_only_copies_and_retains_user_records(tmp_path):
    db, knowledge, manager = stores(tmp_path)
    course = Course(provider="fixture", external_id="one", name="Course")
    task = Task(provider="fixture", external_id="task", course_external_id="one", title="Essay")
    db.apply(
        "fixture",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    db.patch_local(task.id, {"note": "A private note"})
    with db.connection() as connection:
        connection.execute("INSERT INTO mail_messages VALUES ('mail','{}','2026-09-11','{}')")
        connection.execute(
            "INSERT INTO mail_rules VALUES ('rule',?)", (json.dumps({"match": "survey"}),)
        )
        connection.execute(
            "INSERT INTO course_preferences VALUES (?,?)", (course.id, '{"alias":"Writing"}')
        )
        connection.execute(
            "INSERT INTO task_relations VALUES "
            "('link','task',?,'mail','mail','confirmed',1,'[]','before','before')",
            (task.id,),
        )
        connection.execute("INSERT INTO task_relation_history VALUES (1,'link','confirm','before')")
        preserved = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "tasks",
                "task_local_states",
                "mail_messages",
                "mail_rules",
                "course_preferences",
                "task_relations",
                "task_relation_history",
                "task_changes",
                "task_edit_history",
            )
        }
    knowledge.upsert_documents(
        [
            {
                "id": "doc",
                "provider": "fixture",
                "course_id": course.id,
                "title": "Reading",
                "body": "Full material body",
            }
        ]
    )
    chat = knowledge.create_conversation("Discussion", course.id)
    knowledge.add_message(chat, "assistant", "Grounded answer", citations=[{"document_id": "doc"}])
    backup = manager.create_backup()
    folder = manager.folder(backup["id"])
    for name in manager.stores:
        with closing(sqlite3.connect(folder / name)) as connection, connection:
            connection.execute("DROP TABLE schema_migrations")
            if name == "knowledge.sqlite3":
                connection.execute("DROP TABLE material_checkpoints")
                connection.execute("DROP TABLE chat_activity")
                connection.execute("ALTER TABLE conversations DROP COLUMN task_id")
                connection.execute("ALTER TABLE messages DROP COLUMN warnings")
                connection.execute("ALTER TABLE documents DROP COLUMN metadata")
    update_manifest(manager, backup)
    original_hashes = {name: digest(folder / name) for name in (*manager.stores, "manifest.json")}
    listed = manager.list_backups()["backups"]
    assert listed[0]["upgradable"] and not listed[0]["valid"]
    with pytest.raises(RecoveryError, match="Prepare an upgraded copy"):
        manager.restore(backup["id"])
    assert not manager.journal.exists()
    prepared = manager.prepare(backup["id"])
    assert prepared["valid"] and prepared["id"] != backup["id"]
    assert prepared["original_id"] == backup["id"]
    assert original_hashes == {name: digest(folder / name) for name in original_hashes}
    db.patch_local(task.id, {"note": "Current data still active"})
    assert db.tasks()[0]["local"]["note"] == "Current data still active"
    manager.restore(prepared["id"])
    with db.connection() as connection:
        for table, rows in preserved.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
    assert knowledge.get("doc")["body"] == "Full material body"
    message = knowledge.conversation(chat)["messages"][0]
    assert message["content"] == "Grounded answer"
    assert message["citations"] == [{"document_id": "doc"}]
    assert message["warnings"] == []


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA user_version=999",
        "UPDATE schema_migrations SET version=999",
        "CREATE TABLE mystery (id TEXT)",
        "ALTER TABLE documents ADD COLUMN unknown TEXT",
        "DROP TABLE messages",
        "CREATE TRIGGER hidden AFTER INSERT ON documents BEGIN DELETE FROM documents; END",
    ],
)
def test_unknown_future_or_incomplete_backup_never_reaches_preparation(tmp_path, sql):
    _, _, manager = stores(tmp_path)
    backup = manager.create_backup()
    path = manager.folder(backup["id"]) / "knowledge.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(sql)
    update_manifest(manager, backup)
    before = digest(path)
    with pytest.raises(RecoveryError):
        manager.prepare(backup["id"])
    assert digest(path) == before and not manager.journal.exists()
    assert len(list(manager.root.glob("*/manifest.json"))) == 1


def test_failed_upgrade_rolls_back_schema_and_registry_atomically(tmp_path, monkeypatch):
    from coursedeck import migrations

    _, knowledge, _ = stores(tmp_path)
    with knowledge.connection() as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("DROP TABLE material_checkpoints")
        connection.execute("ALTER TABLE documents DROP COLUMN metadata")
    with closing(sqlite3.connect(knowledge.path)) as connection:
        before = structure(connection)
        validate_existing(connection, "knowledge")  # Fill immutable reference cache first.
        monkeypatch.setitem(migrations.SCHEMAS, "material_checkpoints", "CREATE TABLE broken SQL")
        with pytest.raises(sqlite3.Error):
            migrate(connection, "knowledge")
        assert structure(connection) == before
        assert versions(connection) == {}


def test_version_claim_cannot_hide_missing_component(tmp_path):
    db, _, _ = stores(tmp_path)
    with db.connection() as connection:
        connection.execute("DROP TABLE task_relations")
        connection.execute("DROP TABLE task_relation_history")
        with pytest.raises(MigrationError, match="incomplete"):
            migrate(connection, "tasks")


def test_unversioned_missing_half_of_component_is_rejected(tmp_path):
    db, _, _ = stores(tmp_path)
    with db.connection() as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("DROP TABLE task_relation_history")
        with pytest.raises(MigrationError, match="incomplete"):
            migrate(connection, "tasks")


def test_uncheckpointed_backup_cannot_bypass_manifest_checksum(tmp_path):
    _, _, manager = stores(tmp_path)
    backup = manager.create_backup()
    path = manager.folder(backup["id"]) / "knowledge.sqlite3"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO configuration VALUES ('unexpected','{}')")
        writer.commit()
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        with pytest.raises(RecoveryError, match="uncheckpointed"):
            manager.preflight(backup["id"])
        with pytest.raises(RecoveryError, match="uncheckpointed"):
            manager.prepare(backup["id"])
    assert not manager.journal.exists()


def test_malformed_registry_is_rejected_before_querying_its_columns(tmp_path):
    _, knowledge, _ = stores(tmp_path)
    with knowledge.connection() as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("CREATE TABLE schema_migrations (arbitrary TEXT)")
        with pytest.raises(MigrationError, match="registry schema"):
            migrate(connection, "knowledge")


@pytest.mark.parametrize("old_version", [5, 6])
def test_historical_paired_backup_runs_data_migrations_on_copy(tmp_path, old_version):
    db, knowledge, manager = stores(tmp_path)
    course = Course(provider="fixture", external_id="one", name="Original course")
    task = Task(provider="fixture", external_id="one", course_external_id="one", title="Essay")
    db.apply(
        "fixture",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    db.add_course("My course alias", course.provider, course.id)
    db.patch_local(task.id, {"note": "Retain my note"})
    knowledge.upsert_documents(
        [
            {
                "id": "doc",
                "provider": "fixture",
                "course_id": course.id,
                "title": "Material",
                "body": "Retain body",
            }
        ]
    )
    backup = manager.create_backup()
    folder = manager.folder(backup["id"])
    path = folder / "coursedeck.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE schema_migrations")
        if old_version == 5:
            connection.execute("DROP TABLE course_preferences")
        connection.execute(f"PRAGMA user_version={old_version}")
        connection.execute("UPDATE tasks SET archived=1")
    update_manifest(manager, backup)
    original_hash = digest(path)
    prepared = manager.prepare(backup["id"])
    assert digest(path) == original_hash
    manager.restore(prepared["id"])
    assert db.tasks()[0]["local"]["note"] == "Retain my note"
    assert db.tasks()[0]["archived"] is False
    assert db.courses()[0]["name"] == "My course alias"
    assert db.courses()[0]["source_course_ids"] == [course.id]
    assert knowledge.get("doc")["body"] == "Retain body"


def test_historical_data_and_schema_upgrade_roll_back_together(tmp_path, monkeypatch):
    from coursedeck import changes

    db, _, _ = stores(tmp_path)
    course = Course(provider="fixture", external_id="one", name="Original course")
    db.apply("fixture", SyncResult(outcome=Outcome.SUCCESS, courses=[course]), now().isoformat())
    db.add_course("Alias", course.provider, course.id)
    with db.connection() as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("DROP TABLE course_preferences")
        connection.execute("PRAGMA user_version=5")
        before = structure(connection)

    def fail_baseline(connection):
        assert connection.execute("SELECT COUNT(*) FROM course_preferences").fetchone()[0] == 1
        raise RuntimeError("Failure after historical alias migration")

    monkeypatch.setattr(changes, "initialize", fail_baseline)
    with pytest.raises(RuntimeError, match="historical alias"):
        Database(db.path)
    with db.connection() as connection:
        assert structure(connection) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
