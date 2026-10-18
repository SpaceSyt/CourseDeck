import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from keyring.errors import PasswordDeleteError

from coursedeck.credentials import CredentialStore, IncompleteCredentialError


def test_unmocked_vault_is_blocked_before_credential_access(tmp_path):
    store = CredentialStore(tmp_path)
    with pytest.raises(pytest.fail.Exception, match="real OS credential backend"):
        store.get("synthetic")


class MemoryVault:
    def __init__(self):
        self.data = {}
        self.fail = False

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        if self.fail and len(value) == 1000:
            raise RuntimeError("Synthetic write failure")
        assert len(value.encode("utf-16-le")) <= 2560
        self.data[service, name] = value

    def delete_password(self, service, name):
        if self.data.pop((service, name), None) is None:
            raise PasswordDeleteError()


def test_large_tokens_and_rotation_are_atomic(tmp_path):
    vault = MemoryVault()
    store = CredentialStore(tmp_path)
    store.backend = lambda: vault
    store.set("token", {"value": "small"})
    vault.fail = True
    with pytest.raises(RuntimeError):
        store.set("token", {"value": "x" * 8000})
    assert store.get("token") == {"value": "small"}
    vault.fail = False
    store.set("token", {"value": "x" * 8000})
    assert store.get("token") == {"value": "x" * 8000}
    store.delete("token")
    assert not vault.data


def test_missing_credential_chunk_has_specific_recoverable_error(tmp_path):
    vault = MemoryVault()
    store = CredentialStore(tmp_path)
    store.backend = lambda: vault
    store.set("token", {"value": "synthetic" * 1000})
    part = next(key for key in vault.data if key[1].endswith(":0"))
    del vault.data[part]
    with pytest.raises(IncompleteCredentialError, match="record is incomplete"):
        store.get("token")


class PrimarySlotVault(MemoryVault):
    """The Windows backend moves the previous primary entry before each write."""

    def __init__(self):
        super().__init__()
        self.primary = None

    def get_password(self, service, name):
        if self.primary and self.primary[:2] == (service, name):
            return self.primary[2]
        return super().get_password(service, name)

    def set_password(self, service, name, value):
        previous = self.primary
        time.sleep(0.005)
        if previous:
            self.data[previous[:2]] = previous[2]
        self.primary = (service, name, value)

    def delete_password(self, service, name):
        if self.primary and self.primary[:2] == (service, name):
            self.primary = None
            return
        super().delete_password(service, name)


def test_concurrent_sources_do_not_lose_windows_primary_slot_chunks(tmp_path):
    vault = PrimarySlotVault()
    start = threading.Barrier(2)

    def write(name):
        store = CredentialStore(tmp_path)
        store.backend = lambda: vault
        start.wait()
        store.set(name, {"value": name * 4000})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ["source-a", "source-b"]))
    store = CredentialStore(tmp_path)
    store.backend = lambda: vault
    for name in ("source-a", "source-b"):
        assert store.get(name) == {"value": name * 4000}
        store.delete(name)


def test_vault_transaction_serializes_separate_processes(tmp_path):
    started, entered = tmp_path / "started", tmp_path / "entered"
    script = (
        "from pathlib import Path\n"
        "from coursedeck.credentials import CredentialStore\n"
        "import sys\n"
        "directory=Path(sys.argv[1])\n"
        "(directory/'started').touch()\n"
        "with CredentialStore(directory).transaction():\n"
        "    (directory/'entered').touch()\n"
    )
    child = None
    try:
        with CredentialStore(tmp_path).transaction():
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.exists()
            time.sleep(0.1)
            assert not entered.exists()
        child.communicate(timeout=5)
        assert child.returncode == 0
        assert entered.exists()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
