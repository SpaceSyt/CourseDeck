import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from keyring.errors import PasswordDeleteError

from coursedeck.app import create_app
from coursedeck.browser import BrowserManager, SessionPersistenceError
from coursedeck.credentials import CredentialStore, safe_storage_error, storage_failure_details
from coursedeck.db import Database
from coursedeck.gmail_browser import GmailBrowser
from coursedeck.sync import SyncEngine


def failure(code, api):
    exc = RuntimeError("token=SECRET https://school.invalid/?auth=SECRET")
    exc.winerror = code
    exc.funcname = api
    return exc


class MemoryVault:
    def __init__(self):
        self.records = {}
        self.calls = []
        self.write_error_at = None
        self.delete_error = None
        self.write_count = 0

    def get_password(self, service, name):
        return self.records.get(name)

    def set_password(self, service, name, value):
        self.write_count += 1
        self.calls.append("write")
        if self.write_count == self.write_error_at:
            raise failure(112, "CredWrite")
        self.records[name] = value

    def delete_password(self, service, name):
        self.calls.append("delete")
        if self.delete_error:
            raise self.delete_error
        if self.records.pop(name, None) is None:
            raise PasswordDeleteError()


@pytest.fixture
def stored(tmp_path):
    vault = MemoryVault()
    store = CredentialStore(tmp_path)
    store.backend = lambda: vault
    store.set("fixture", {"old": True})
    vault.calls.clear()
    vault.write_count = 0
    return store, vault


@pytest.mark.parametrize(
    "write_at,step,deletions", [(1, "write_chunk", 0), (3, "publish_snapshot", 2)]
)
def test_write_failure_keeps_old_snapshot_and_existing_cleanup_order(
    stored, write_at, step, deletions
):
    store, vault = stored
    vault.write_error_at = write_at
    with pytest.raises(RuntimeError) as caught:
        store.set("fixture", {"new": "x" * 1100})
    details = storage_failure_details(caught.value)
    assert details["failed_step"] == step
    assert details["snapshot_published"] is False
    assert details["primary_error"] == {"api": "CredWrite", "winerror": 112, "type": "RuntimeError"}
    assert "cleanup_errors" not in details
    assert store.get("fixture") == {"old": True}
    assert vault.calls == ["write"] * write_at + ["delete"] * deletions
    assert "SECRET" not in json.dumps(details)


def test_rollback_preserves_primary_reason_and_stops_at_same_cleanup_failure(stored):
    store, vault = stored
    vault.write_error_at = 3
    vault.delete_error = failure(5, "CredDelete")
    with pytest.raises(RuntimeError) as caught:
        store.set("fixture", {"new": "x" * 1100})
    details = storage_failure_details(caught.value)
    assert details["failed_step"] == "publish_snapshot"
    assert details["primary_error"]["winerror"] == 112
    assert details["cleanup_errors"][0]["failed_step"] == "rollback_cleanup"
    assert details["cleanup_errors"][0]["error"]["winerror"] == 5
    assert details["snapshot_published"] is False
    assert vault.calls == ["write", "write", "write", "delete"]
    assert store.get("fixture") == {"old": True}
    assert "SECRET" not in json.dumps(details)


def test_cleanup_failure_reports_that_new_snapshot_is_already_published(stored):
    store, vault = stored
    vault.delete_error = failure(5, "CredDelete")
    with pytest.raises(RuntimeError) as caught:
        store.set("fixture", {"new": True})
    details = storage_failure_details(caught.value)
    assert details["failed_step"] == "previous_cleanup"
    assert details["snapshot_published"] is True
    assert details["primary_error"]["api"] == "CredDelete"
    assert store.get("fixture") == {"new": True}
    assert vault.calls == ["write", "write", "delete"]


@pytest.mark.skipif(os.name != "nt", reason="Windows file lock")
@pytest.mark.parametrize("release", [False, True])
def test_lock_errors_have_exact_phase_without_real_vault(stored, monkeypatch, release):
    import msvcrt

    store, vault = stored

    def lock(fd, mode, count):
        if (mode == msvcrt.LK_UNLCK) == release:
            raise OSError(36, "SECRET")

    monkeypatch.setattr(msvcrt, "locking", lock)
    with pytest.raises(OSError) as caught:
        store.set("fixture", {"new": True})
    details = storage_failure_details(caught.value)
    assert details["failed_step"] == ("lock_release" if release else "lock_acquire")
    assert details["primary_error"]["errno"] == 36
    assert details["snapshot_published"] is release
    assert bool(vault.calls) is release


def test_diagnostics_do_not_accept_arbitrary_strings_or_attribute_payloads():
    exc = failure("SECRET", "CredWrite?token=SECRET")
    exc.errno = True
    exc._coursedeck_storage_attempt = {"failed_step": "SECRET"}
    assert safe_storage_error(exc) == {"api": "unknown", "type": "RuntimeError"}
    assert "SECRET" not in json.dumps(storage_failure_details(exc))


@pytest.mark.skipif(os.name != "nt", reason="Windows file lock")
def test_unlock_failure_does_not_erase_write_and_rollback_failures(stored, monkeypatch):
    import msvcrt

    store, vault = stored
    vault.write_error_at = 3
    vault.delete_error = failure(5, "CredDelete")

    def lock(fd, mode, count):
        if mode == msvcrt.LK_UNLCK:
            raise OSError(36, "SECRET")

    monkeypatch.setattr(msvcrt, "locking", lock)
    with pytest.raises(OSError) as caught:
        store.set("fixture", {"new": "x" * 1100})
    details = storage_failure_details(caught.value)
    assert details["primary_error"]["winerror"] == 112
    assert [x["failed_step"] for x in details["cleanup_errors"]] == [
        "rollback_cleanup",
        "lock_release",
    ]
    assert details["cleanup_errors"][0]["error"]["winerror"] == 5
    assert details["cleanup_errors"][1]["error"]["errno"] == 36
    assert vault.calls == ["write", "write", "write", "delete"]


@pytest.mark.parametrize("operation", ["sync", "disconnect"])
async def test_gmail_clears_obsolete_diagnostic_on_recovery(tmp_path, monkeypatch, operation):
    db = Database(tmp_path / "db")
    gmail = GmailBrowser(db, tmp_path)
    monkeypatch.setattr(
        gmail,
        "_sync",
        AsyncMock(
            side_effect=SessionPersistenceError(
                operation="save",
                cause=failure(5, "CredWrite"),
            )
        ),
    )
    db.update_state("gmail", metadata={"keep": True})
    await gmail.sync()
    assert (
        db.state("gmail")["metadata"]["session_error"]["storage"]["primary_error"]["winerror"] == 5
    )
    monkeypatch.setattr(gmail, "_sync", AsyncMock())
    monkeypatch.setattr(gmail.browser, "reset", AsyncMock())
    await getattr(gmail, operation)()
    assert db.state("gmail")["metadata"] == {"keep": True}
    assert db.state("gmail")["error_code"] is None


async def test_browser_and_sync_preserve_safe_details_without_secrets(tmp_path):
    store = CredentialStore(tmp_path)
    vault = MemoryVault()
    vault.write_error_at = 1
    store.backend = lambda: vault
    browser = BrowserManager(tmp_path, "rephactor", vault=store)
    db = Database(tmp_path / "db")
    source = SimpleNamespace(
        key="fixture",
        connection_status=lambda: "connected",
        sync=lambda: browser._write_session([]),
        close=AsyncMock(),
    )
    engine = SyncEngine(db, [source])
    try:
        await engine.sync_one("fixture")
        state = db.state("fixture")
        diagnostic = state["metadata"]["session_error"]
        assert diagnostic["category"] == "session_save_failed"
        assert diagnostic["storage"]["failed_step"] == "write_chunk"
        assert diagnostic["storage"]["primary_error"]["winerror"] == 112
        assert "SECRET" not in json.dumps(state)
    finally:
        await engine.close()


async def test_api_serializes_only_safe_storage_details(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    exc = SessionPersistenceError(operation="save", cause=failure(5, "CredWrite"))
    monkeypatch.setattr(app.state.gmail, "connect", AsyncMock(side_effect=exc))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/mail/connection/connect", headers={"X-CourseDeck": "1"}
            )
        assert response.status_code == 503
        assert response.json()["storage"]["primary_error"]["winerror"] == 5
        assert response.json()["storage"]["snapshot_published"] is None
        assert "SECRET" not in response.text
    finally:
        await app.state.gmail.close()
        await app.state.engine.close()
        app.state.close_readers()
