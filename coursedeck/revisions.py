"""Cheap process-local invalidation for SQLite readers, including external writers."""

import sqlite3
from pathlib import Path
from threading import RLock


class DatabaseRevision:
    """Observe commits without holding a read transaction or relying on write hooks.

    Tokens are meaningful only for this observer. A new observer or reopened file
    has a different generation; never persist these tokens as data versions.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = RLock()
        self._connection = None
        self._identity = None
        self._generation = 0

    def token(self):
        with self._lock:
            stat = self.path.stat()
            identity = (
                (stat.st_dev, stat.st_ino, stat.st_birthtime_ns)
                if hasattr(stat, "st_birthtime_ns")
                else (stat.st_dev, stat.st_ino)
            )
            if self._connection is None or identity != self._identity:
                self.close()
                self._connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                    isolation_level=None,
                    timeout=10,
                )
                self._identity = identity
                self._generation += 1
            version = self._connection.execute("PRAGMA data_version").fetchone()[0]
            schema = self._connection.execute("PRAGMA schema_version").fetchone()[0]
            return self._generation, identity, version, schema

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
