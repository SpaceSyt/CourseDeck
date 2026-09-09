import json
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from coursedeck.connectors.classroom import ClassroomConnector, ClassroomTransport, map_task
from coursedeck.credentials import CredentialStore
from coursedeck.db import Database
from coursedeck.domain import Outcome


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/classroom/coursework.json").read_text())


def test_mapper_utc_score_and_submission():
    task = map_task("c", fixture(), {"state": "TURNED_IN", "assignedGrade": 0, "late": True})
    assert task.due_at.isoformat() == "2026-11-01T06:30:00+00:00"
    assert task.score == 0 and task.graded
    assert task.submission_status == "submitted" and task.raw_data["submission"]["late"]
    assert task.closes_at is None
    assert map_task("c", {"id": "a", "title": "Undated"}, None).due_at is None


@pytest.mark.parametrize("failure", [None, 401, 403, 429, 500])
async def test_pagination_and_failure_are_explicit(failure):
    def handler(request):
        if request.url.path == "/v1/courses":
            if request.url.params.get("pageToken"):
                return httpx.Response(failure or 200, json={} if failure else {"courses": []})
            return httpx.Response(
                200, json={"courses": [{"id": "c", "name": "Writing"}], "nextPageToken": "second"}
            )
        if "studentSubmissions" in request.url.path:
            return httpx.Response(
                200, json={"studentSubmissions": [{"courseWorkId": "a1", "state": "TURNED_IN"}]}
            )
        return httpx.Response(200, json={"courseWork": [fixture()]})

    async with httpx.AsyncClient(
        base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ClassroomTransport(client).sync()
    assert len(result.tasks) == 1
    assert result.tasks[0].submission_status == "submitted"
    assert result.complete == (failure is None)
    assert result.outcome == (Outcome.PARTIAL if failure else Outcome.SUCCESS)


async def test_empty_is_distinct_from_rejected_login():
    for code, outcome in [
        (200, Outcome.SUCCESS),
        (401, Outcome.AUTH_REQUIRED),
        (429, Outcome.RATE_LIMITED),
    ]:
        async with httpx.AsyncClient(
            base_url="https://example.test/",
            transport=httpx.MockTransport(lambda r, code=code: httpx.Response(code, json={})),
        ) as client:
            result = await ClassroomTransport(client).sync()
        assert result.outcome == outcome
        assert result.complete == (code == 200)


async def test_oauth_state_expiry_and_disconnect(tmp_path):
    vault = Mock()
    connector = ClassroomConnector(Database(tmp_path / "db"), vault)
    connector.pending = (Mock(), "correct", time.monotonic() + 10)
    with pytest.raises(ValueError):
        await connector.authorization_callback({"state": "wrong", "code": "secret"})
    vault.set.assert_not_called()
    connector.pending = (Mock(), "correct", time.monotonic() - 1)
    with pytest.raises(ValueError):
        await connector.authorization_callback({"state": "correct", "code": "secret"})
    await connector.disconnect()
    assert connector.pending is None
    vault.delete.assert_called_once_with("google_classroom")


def test_plaintext_keyring_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("keyring.get_keyring", lambda: Mock(backends=[]))
    with pytest.raises(RuntimeError):
        CredentialStore(tmp_path).set("token", {"refresh_token": "test"})


async def test_submission_rate_limit_keeps_already_read_coursework():
    def handler(request):
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json={"courses": [{"id": "c", "name": "Writing"}]})
        if "studentSubmissions" in request.url.path:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"courseWork": [fixture()]})

    async with httpx.AsyncClient(
        base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ClassroomTransport(client).sync()
    assert result.outcome == Outcome.PARTIAL and not result.complete
    assert len(result.tasks) == 1 and result.tasks[0].due_at is not None
    assert result.tasks[0].submission_status == "unknown"
    assert result.metadata["last_error"] == Outcome.RATE_LIMITED


async def test_json_error_envelope_is_not_an_empty_success():
    async with httpx.AsyncClient(
        base_url="https://example.test/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "unexpected"})),
    ) as client:
        result = await ClassroomTransport(client).sync()
    assert result.outcome == Outcome.PARSE_ERROR and not result.complete
