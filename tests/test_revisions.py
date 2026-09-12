import sqlite3
from concurrent.futures import ThreadPoolExecutor

from coursedeck.revisions import DatabaseRevision


def database(path, value=1):
    with sqlite3.connect(path) as db:
        db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE facts(value INTEGER);")
        db.execute("INSERT INTO facts VALUES (?)", (value,))


def test_revision_observes_external_commits_schema_and_backup_restore(tmp_path):
    path, backup = tmp_path / "live.db", tmp_path / "backup.db"
    database(path)
    database(backup, 2)
    revision = DatabaseRevision(path)
    try:
        initial = revision.token()
        assert revision.token() == initial
        with sqlite3.connect(path) as db:
            db.execute("UPDATE facts SET value=3")
        updated = revision.token()
        assert updated != initial
        with sqlite3.connect(backup) as source, sqlite3.connect(path) as target:
            source.backup(target)
        restored = revision.token()
        assert restored != updated
        with sqlite3.connect(path) as db:
            db.execute("CREATE INDEX fact_value ON facts(value)")
        assert revision.token() != restored
        # Readers do not pin a transaction or prevent WAL checkpoints.
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        revision.close()


def test_revision_is_thread_safe_and_reopening_invalidates(tmp_path):
    path = tmp_path / "live.db"
    database(path)
    revision = DatabaseRevision(path)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            tokens = list(executor.map(lambda _: revision.token(), range(50)))
        assert len(set(tokens)) == 1
        revision.close()
        assert revision.token() != tokens[0]
    finally:
        revision.close()
