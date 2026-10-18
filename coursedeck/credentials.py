import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from uuid import uuid4

import keyring
from keyring.errors import PasswordDeleteError


class IncompleteCredentialError(RuntimeError):
    """A published credential snapshot is missing one or more stored chunks."""


def safe_storage_error(exc):
    api = getattr(exc, "funcname", None)
    result = {"api": api if api in ("CredRead", "CredWrite", "CredDelete") else "unknown"}
    for key in ("winerror", "errno"):
        value = getattr(exc, key, None)
        if type(value) is int and -(2**31) <= value < 2**32:
            result[key] = value
    name = type(exc).__name__
    result["type"] = (
        name
        if name
        in {
            "OSError",
            "PermissionError",
            "RuntimeError",
            "ValueError",
            "TypeError",
            "AttributeError",
            "JSONDecodeError",
            "IncompleteCredentialError",
            "error",
        }
        else "other"
    )
    return result


@dataclass
class _StorageAttempt:
    published: bool = False
    primary: Exception | None = None
    failed_step: str = "unknown"
    cleanup: list[tuple[str, Exception]] = field(default_factory=list)

    def record(self, step, exc):
        if self.primary is None:
            self.primary, self.failed_step = exc, step
        elif exc is not self.primary and not any(exc is error for _, error in self.cleanup):
            self.cleanup.append((step, exc))

    def safe_details(self):
        result = {
            "failed_step": self.failed_step,
            "snapshot_published": self.published,
            "primary_error": safe_storage_error(self.primary),
        }
        if self.cleanup:
            result["cleanup_errors"] = [
                {"failed_step": step, "error": safe_storage_error(exc)}
                for step, exc in self.cleanup
            ]
        return result


_storage_attempt = ContextVar("coursedeck_storage_attempt", default=None)


def storage_failure_details(exc):
    attempt = getattr(exc, "_coursedeck_storage_attempt", None)
    if isinstance(attempt, _StorageAttempt):
        return attempt.safe_details()
    return {
        "failed_step": "unknown",
        "snapshot_published": None,
        "primary_error": safe_storage_error(exc),
    }


def _diagnosed(step, operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except Exception as exc:
        if (attempt := _storage_attempt.get()) is not None:
            attempt.record(step, exc)
        raise


def serialized(operation):
    @wraps(operation)
    def run(self, *args, **kwargs):
        attempt = _StorageAttempt()
        token = _storage_attempt.set(attempt)
        try:
            with self.transaction():
                return _diagnosed(operation.__name__, operation, self, *args, **kwargs)
        except Exception as exc:
            if attempt.primary is None:
                attempt.record("lock_open", exc)
            exc._coursedeck_storage_attempt = attempt
            raise
        finally:
            _storage_attempt.reset(token)

    return run


class CredentialStore:
    """OS vault only. Never silently fall back to plaintext keyrings."""

    def __init__(self, data_dir: Path):
        self.directory = data_dir.resolve()
        suffix = hashlib.sha256(str(self.directory).encode()).hexdigest()[:16]
        self.service = f"CourseDeck-{suffix}"

    @contextmanager
    def transaction(self):
        # Windows keyring moves one shared primary credential between usernames.
        # Lock the whole service transaction, including chunk reads and cleanup.
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "credentials.lock").open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                _diagnosed("lock_acquire", msvcrt.locking, handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    _diagnosed("lock_release", msvcrt.locking, handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                _diagnosed("lock_acquire", fcntl.flock, handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    _diagnosed("lock_release", fcntl.flock, handle, fcntl.LOCK_UN)

    def backend(self):
        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        for item in candidates:
            if (
                type(item).__module__
                in {
                    "keyring.backends.Windows",
                    "keyring.backends.macOS",
                    "keyring.backends.SecretService",
                    "keyring.backends.kwallet",
                }
                and item.priority > 0
            ):
                return item
        raise RuntimeError(
            "No secure OS credential store is available; plaintext fallback disabled"
        )

    @serialized
    def get(self, name: str) -> dict | None:
        backend = self.backend()
        value = backend.get_password(self.service, name)
        if value is None:
            return None
        document = json.loads(value)
        if "_coursedeck_chunks" not in document:
            return document
        parts = [backend.get_password(self.service, part) for part in self._parts(name, document)]
        if any(part is None for part in parts):
            raise IncompleteCredentialError(
                "Credential vault record is incomplete. Reconnect the source."
            )
        return json.loads("".join(parts))

    @serialized
    def set(self, name: str, value: dict):
        backend = _diagnosed("backend", self.backend)
        old = _diagnosed("read_previous", backend.get_password, self.service, name)
        payload = _diagnosed(
            "serialize", json.dumps, value, separators=(",", ":"), ensure_ascii=True
        )
        # Windows generic credentials have a small per-item blob limit. Publish a generation
        # pointer only after all chunks are stored, preserving the previous token on failure.
        manifest = {"_coursedeck_chunks": (len(payload) + 999) // 1000, "generation": uuid4().hex}
        parts = _diagnosed("prepare_chunks", self._parts, name, manifest)
        written = []
        try:
            for index, part in enumerate(parts):
                _diagnosed(
                    "write_chunk",
                    backend.set_password,
                    self.service,
                    part,
                    payload[index * 1000 : (index + 1) * 1000],
                )
                written.append(part)
            _diagnosed(
                "publish_snapshot", backend.set_password, self.service, name, json.dumps(manifest)
            )
            _storage_attempt.get().published = True
        except Exception:
            for part in written:
                _diagnosed("rollback_cleanup", self._delete, backend, part)
            raise
        if old:
            previous = _diagnosed("decode_previous", json.loads, old)
            for part in _diagnosed("previous_chunks", self._parts, name, previous):
                _diagnosed("previous_cleanup", self._delete, backend, part)

    @serialized
    def delete(self, name: str):
        backend = self.backend()
        old = backend.get_password(self.service, name)
        self._delete(backend, name)
        if old:
            for part in self._parts(name, json.loads(old)):
                self._delete(backend, part)

    @staticmethod
    def _parts(name: str, manifest: dict) -> list[str]:
        count = manifest.get("_coursedeck_chunks", 0)
        if not isinstance(count, int) or not 0 <= count <= 128:
            raise ValueError("Invalid credential chunk count")
        return [f"{name}:{manifest['generation']}:{index}" for index in range(count)]

    def _delete(self, backend, name: str):
        try:
            backend.delete_password(self.service, name)
        except PasswordDeleteError:
            pass
