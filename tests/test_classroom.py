import json
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from coursedeck.connectors.classroom import (
    ClassroomConnector,
    ClassroomTransport,
    map_course,
    map_task,
)
from coursedeck.credentials import CredentialStore
from coursedeck.db import Database
from coursedeck.domain import Outcome, SyncResult


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


async def test_unread_submissions_retain_cached_grades_without_claiming_freshness(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    course = map_course({"id": "c", "name": "Writing"})
    previous = map_task("c", fixture(), {"state": "TURNED_IN", "assignedGrade": 0})
    db.apply(
        "google_classroom",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[previous]),
        "2026-10-01T00:00:00+00:00",
    )

    def handler(request):
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json={"courses": [{"id": "c", "name": "Writing"}]})
        if "studentSubmissions" in request.url.path:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"courseWork": [fixture()]})

    async with httpx.AsyncClient(
        base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ClassroomTransport(client).sync()
    assert result.outcome == Outcome.PARTIAL
    db.apply("google_classroom", result, "2026-10-02T00:00:00+00:00")
    cached = db.tasks()[0]
    assert cached["score"] == 0 and cached["graded"] is True
    assert cached["submission_status"] == "submitted"
    assert cached["field_availability"]["score"] == "unread"
    assert cached["source_state"] == "unknown"
    assert not cached["is_completed"]


async def test_malformed_records_do_not_stop_other_coursework_or_courses():
    def handler(request):
        if request.url.path.endswith("/courses"):
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {"id": "broken"},
                        {"id": "c", "name": "Writing"},
                        {"id": "d", "name": "Math"},
                    ]
                },
            )
        if "studentSubmissions" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "studentSubmissions": [
                        None,
                        {"courseWorkId": "a1", "state": "TURNED_IN"},
                    ]
                },
            )
        return httpx.Response(200, json={"courseWork": [{"id": "broken"}, fixture()]})

    async with httpx.AsyncClient(
        base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ClassroomTransport(client).sync()
    assert result.outcome == Outcome.PARTIAL and not result.complete
    assert {task.course_external_id for task in result.tasks} == {"c", "d"}
    assert all(task.submission_status == "submitted" for task in result.tasks)
    assert result.warnings
