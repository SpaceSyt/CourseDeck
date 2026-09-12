import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from scripts.smoke_support import stop_backend


@pytest.mark.skipif(os.name != "nt", reason="Windows venv redirector and WAL handle lifecycle")
def test_stop_waits_for_python_descendants_before_reopening_wal(tmp_path):
    code = """
import sqlite3, sys, time
from pathlib import Path
from coursedeck.revisions import DatabaseRevision
path, ready = map(Path, sys.argv[1:])
connection = sqlite3.connect(path)
connection.executescript('PRAGMA journal_mode=WAL; CREATE TABLE facts(value INTEGER);')
connection.execute('INSERT INTO facts VALUES (1)')
connection.commit()
observer = DatabaseRevision(path)
observer.token()
connection.close()
ready.write_text('ready')
time.sleep(60)
"""
    for attempt in range(3):
        path, ready = tmp_path / f"{attempt}.db", tmp_path / f"{attempt}.ready"
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(ready)],
            env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), "Disposable observer process failed to start"
            stop_backend(process)
            # No sleep/retry: shutdown must release every backend process first.
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO facts VALUES (2)")
                assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
        finally:
            stop_backend(process)
