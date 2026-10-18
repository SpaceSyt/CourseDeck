import pytest
from playwright.async_api import TimeoutError as BrowserTimeout

from coursedeck.db import Database
from coursedeck.gmail_browser import GmailBrowser, GmailLoginRequired


@pytest.mark.parametrize(
    "failure,code,authorized",
    [
        (BrowserTimeout("Hidden body"), "network_error", True),
        (ValueError("Unexpected layout"), "read_error", True),
        (GmailLoginRequired(), "auth_required", False),
    ],
)
async def test_only_authentication_failures_request_reconnect(tmp_path, failure, code, authorized):
    db = Database(tmp_path / "db")
    db.update_state("gmail", authorized=True, last_sync="old")
    adapter = GmailBrowser(db, tmp_path)

    async def fail():
        raise failure

    adapter._sync = fail
    await adapter.sync()
    state = db.state("gmail")
    assert state["authorized"] is authorized
    assert state["error_code"] == code and state["last_sync"] == "old"
    assert ("Reconnect" in state["error"]) is (code == "auth_required")
