import json
from contextlib import closing

import pytest

from coursedeck.db import Database
from coursedeck.domain import Settings
from coursedeck.knowledge import KnowledgeStore
from coursedeck.recovery import RecoveryError, RecoveryManager, digest


def setup(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    manager = RecoveryManager(tmp_path, db, knowledge)
    return db, knowledge, manager


def document(body):
    return {
        "id": "a",
        "provider": "fixture",
        "course_id": "fixture:1",
        "title": "Notes",
        "body": body,
    }


def test_paired_backup_restores_both_stores_and_keeps_pre_restore_pair(tmp_path):
    db, knowledge, manager = setup(tmp_path)
    knowledge.upsert_documents([document("Original")])
    backup = manager.create_backup()
    knowledge.upsert_documents([document("Later")])
    db.save_settings(Settings(startup_sync=False))
    assert manager.preflight(backup["id"])["valid"]
    restored = manager.restore(backup["id"])
    assert knowledge.get("a")["body"] == "Original" and db.settings().startup_sync
    assert manager.preflight(restored["pre_restore_backup"])["reason"] == "pre_restore"
    assert not manager.journal.exists()
    assert len(manager.list_backups()["backups"]) == 2


@pytest.mark.parametrize("backup_id", ["../outside", "C:/temp/db", "..", "a/b", ""])
def test_restore_rejects_arbitrary_paths(tmp_path, backup_id):
    _, _, manager = setup(tmp_path)
    with pytest.raises(RecoveryError):
        manager.restore(backup_id)
    assert not manager.journal.exists()


def test_restore_revalidates_checksum_after_ui_preflight(tmp_path):
    db, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    manager.preflight(backup["id"])
    path = manager.folder(backup["id"]) / "knowledge.sqlite3"
    with path.open("ab") as stream:
        stream.write(b"changed after preview")
    with pytest.raises(RecoveryError, match="checksum"):
        manager.restore(backup["id"])
    assert db.settings().startup_sync and not manager.journal.exists()


def test_schema_mismatch_is_rejected_even_with_updated_checksum(tmp_path):
    import sqlite3

    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    folder = manager.folder(backup["id"])
    path = folder / "knowledge.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE messages")
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"][path.name]["sha256"] = digest(path)
    manifest["files"][path.name]["bytes"] = path.stat().st_size
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RecoveryError, match="schema"):
        manager.restore(backup["id"])


def test_corrupt_sqlite_is_rejected_even_if_manifest_hash_matches(tmp_path):
    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    folder = manager.folder(backup["id"])
    path = folder / "knowledge.sqlite3"
    path.write_bytes(b"This is not a SQLite database")
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"][path.name]["sha256"] = digest(path)
    manifest["files"][path.name]["bytes"] = path.stat().st_size
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RecoveryError, match="validated"):
        manager.restore(backup["id"])
    assert not manager.journal.exists()


def test_failed_restore_rolls_back_both_databases(tmp_path, monkeypatch):
    db, knowledge, manager = setup(tmp_path)
    knowledge.upsert_documents([document("Original")])
    backup = manager.create_backup()
    knowledge.upsert_documents([document("Current")])
    db.save_settings(Settings(startup_sync=False))
    original = manager._copy_pair
    calls = 0

    def fail_once(backup_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            db.save_settings(Settings(startup_sync=True))
            raise OSError("disk error")
        original(backup_id)

    monkeypatch.setattr(manager, "_copy_pair", fail_once)
    with pytest.raises(RecoveryError, match="rolled back"):
        manager.restore(backup["id"])
    assert knowledge.get("a")["body"] == "Current" and not db.settings().startup_sync
    assert calls == 2 and not manager.journal.exists()


def test_failed_rollback_leaves_durable_recovery_marker(tmp_path, monkeypatch):
    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()

    def fail(backup_id):
        raise OSError("disk error")

    monkeypatch.setattr(manager, "_copy_pair", fail)
    with pytest.raises(RecoveryError, match="Keep maintenance active"):
        manager.restore(backup["id"])
    assert manager.list_backups()["interrupted_restore"]
    assert json.loads(manager.journal.read_text())["rollback"] != backup["id"]


def legacy_backup(manager, db, knowledge, stamp="20260910T010000000000Z"):
    db.backup(manager.root / f"coursedeck-{stamp}.sqlite3")
    knowledge.backup(manager.root / f"knowledge-{stamp}.sqlite3")
    return "legacy-" + stamp


def test_legacy_pair_is_listed_imported_and_restorable_without_changing_originals(tmp_path):
    db, knowledge, manager = setup(tmp_path)
    knowledge.upsert_documents([document("Legacy body")])
    legacy_id = legacy_backup(manager, db, knowledge)
    original_hashes = {path.name: digest(path) for path in manager.root.glob("*.sqlite3")}
    listed = manager.list_backups()["backups"]
    assert listed == [{"id": legacy_id, "valid": False, "legacy": True, "importable": True}]
    imported = manager.import_legacy(legacy_id)
    assert imported["valid"] and imported["reason"] == "legacy_import"
    knowledge.upsert_documents([document("Current")])
    manager.restore(imported["id"])
    assert knowledge.get("a")["body"] == "Legacy body"
    assert {path.name: digest(path) for path in manager.root.glob("*.sqlite3")} == original_hashes


def test_missing_and_incompatible_legacy_pairs_are_visible_but_not_importable(tmp_path):
    import sqlite3

    db, knowledge, manager = setup(tmp_path)
    legacy_id = legacy_backup(manager, db, knowledge)
    path = manager.root / "knowledge-20260910T010000000000Z.sqlite3"
    with closing(sqlite3.connect(path)) as source, source:
        source.execute("DROP TABLE messages")
    db.backup(manager.root / "coursedeck-20260911T010000Z.sqlite3")
    listed = manager.list_backups()["backups"]
    assert len(listed) == 2
    assert any("schema" in row.get("error", "") for row in listed)
    assert any("missing" in row.get("error", "") for row in listed)
    assert not any(row.get("importable") for row in listed)
    with pytest.raises(RecoveryError, match="migration is unsupported"):
        manager.import_legacy(legacy_id)


@pytest.mark.parametrize(
    "payload",
    [None, [], 1, "manifest", {}, {"files": []}, {"files": {}, "created_at": [], "reason": {}}],
)
def test_invalid_manifest_shapes_raise_safe_recovery_error(tmp_path, payload):
    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    (manager.folder(backup["id"]) / "manifest.json").write_text(json.dumps(payload))
    with pytest.raises(RecoveryError):
        manager.preflight(backup["id"])
    assert manager.list_backups()["backups"][0]["valid"] is False


@pytest.mark.parametrize(
    "info", [[], None, {"bytes": "1"}, {"bytes": 1, "sha256": [], "schema": {}}]
)
def test_invalid_manifest_file_metadata_is_rejected(tmp_path, info):
    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    backup["files"]["knowledge.sqlite3"] = info
    (manager.folder(backup["id"]) / "manifest.json").write_text(json.dumps(backup))
    with pytest.raises(RecoveryError, match="metadata"):
        manager.preflight(backup["id"])


def test_rollback_verification_failure_keeps_journal(tmp_path, monkeypatch):
    from coursedeck import recovery

    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    original_copy, original_inspect = manager._copy_pair, recovery.inspect_database
    copies = 0

    def fail_first(backup_id):
        nonlocal copies
        copies += 1
        if copies == 1:
            raise OSError("restore failed")
        original_copy(backup_id)

    def invalid_rollback(path):
        if copies == 2:
            raise RecoveryError("Rollback failed verification")
        return original_inspect(path)

    monkeypatch.setattr(manager, "_copy_pair", fail_first)
    monkeypatch.setattr(recovery, "inspect_database", invalid_rollback)
    with pytest.raises(RecoveryError, match="Keep maintenance active"):
        manager.restore(backup["id"])
    assert manager.journal.exists() and copies == 2


@pytest.mark.parametrize("reason", ["manual", "pre_restore"])
def test_pending_recovery_cannot_certify_live_databases_as_a_backup(tmp_path, reason):
    _, _, manager = setup(tmp_path)
    manager.journal.write_text('{"interrupted": true}')
    with pytest.raises(RecoveryError, match="Recovery is pending"):
        manager.create_backup(reason)
    assert not list(manager.root.glob("*/manifest.json"))


def test_pending_mixed_pair_failure_preserves_original_rollback_and_marker(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    from coursedeck.domain import Course, Outcome, SyncResult, Task, now

    db, knowledge, manager = setup(tmp_path)
    course = Course(provider="fixture", external_id="1", name="Course")
    task = Task(provider="fixture", course_external_id="1", external_id="1", title="Task")
    db.apply(
        "fixture",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    db.patch_local(task.id, {"note": "A task"})
    knowledge.upsert_documents([document("A knowledge")])
    pair_a = manager.create_backup()
    db.patch_local(task.id, {"note": "B task"})
    knowledge.upsert_documents([document("B knowledge")])
    pair_b = manager.create_backup()
    # Simulate interruption after copying only A's task database over paired B.
    with closing(sqlite3.connect(manager.folder(pair_a["id"]) / "coursedeck.sqlite3")) as source:
        with closing(sqlite3.connect(db.path)) as target:
            source.backup(target)
    assert db.tasks()[0]["local"]["note"] == "A task"
    assert knowledge.get("a")["body"] == "B knowledge"
    journal = json.dumps({"target": pair_a["id"], "rollback": pair_b["id"]})
    manager.journal.write_text(journal)
    original = manager._copy_pair
    copied = []

    def fail_target(backup_id):
        copied.append(backup_id)
        if backup_id == pair_a["id"]:
            raise OSError("Target copy failed")
        original(backup_id)

    monkeypatch.setattr(manager, "_copy_pair", fail_target)
    with pytest.raises(RecoveryError, match="recovery remains pending"):
        manager.restore(pair_a["id"])
    assert copied == [pair_a["id"], pair_b["id"]]
    assert db.tasks()[0]["local"]["note"] == "B task"
    assert knowledge.get("a")["body"] == "B knowledge"
    assert manager.journal.read_text() == journal
    assert len(list(manager.root.glob("*/manifest.json"))) == 2


@pytest.mark.parametrize("journal", ["not json", "[]", '{"rollback": "../outside"}'])
def test_bad_pending_journal_failure_stays_blocked_without_new_backup(
    tmp_path, monkeypatch, journal
):
    _, _, manager = setup(tmp_path)
    backup = manager.create_backup()
    manager.journal.write_text(journal)

    def fail(backup_id):
        raise OSError("Target copy failed")

    monkeypatch.setattr(manager, "_copy_pair", fail)
    with pytest.raises(RecoveryError, match="Keep maintenance active"):
        manager.restore(backup["id"])
    assert manager.journal.read_text() == journal
    assert len(list(manager.root.glob("*/manifest.json"))) == 1


def test_pending_recovery_can_succeed_without_a_known_rollback(tmp_path):
    _, knowledge, manager = setup(tmp_path)
    knowledge.upsert_documents([document("Original")])
    backup = manager.create_backup()
    knowledge.upsert_documents([document("Mixed")])
    manager.journal.write_text("unknown journal format")
    result = manager.restore(backup["id"])
    assert result["pre_restore_backup"] is None
    assert knowledge.get("a")["body"] == "Original"
    assert not manager.journal.exists()
    assert len(list(manager.root.glob("*/manifest.json"))) == 1
