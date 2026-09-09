import hashlib
import json
from pathlib import Path
from uuid import uuid4

import keyring
from keyring.errors import PasswordDeleteError


class CredentialStore:
    """OS vault only. Never silently fall back to plaintext keyrings."""

    def __init__(self, data_dir: Path):
        suffix = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:16]
        self.service = f"CourseDeck-{suffix}"

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
            raise RuntimeError("Credential vault record is incomplete. Reconnect the source.")
        return json.loads("".join(parts))

    def set(self, name: str, value: dict):
        backend = self.backend()
        old = backend.get_password(self.service, name)
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        # Windows generic credentials have a small per-item blob limit. Publish a generation
        # pointer only after all chunks are stored, preserving the previous token on failure.
        manifest = {"_coursedeck_chunks": (len(payload) + 999) // 1000, "generation": uuid4().hex}
        parts = self._parts(name, manifest)
        written = []
        try:
            for index, part in enumerate(parts):
                backend.set_password(self.service, part, payload[index * 1000 : (index + 1) * 1000])
                written.append(part)
            backend.set_password(self.service, name, json.dumps(manifest))
        except Exception:
            for part in written:
                self._delete(backend, part)
            raise
        if old:
            for part in self._parts(name, json.loads(old)):
                self._delete(backend, part)

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
