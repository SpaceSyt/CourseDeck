from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.connectors.http import TransportError
from coursedeck.connectors.rephactor import (
    DASHBOARD,
    HOME,
    RephactorConnector,
    parse_assignments,
    parse_course,
)
from coursedeck.db import Database
from coursedeck.domain import Outcome, SyncResult


def course_record(key="course-a"):
    return {"courseId": key, "title": "Programming", "courseNumber": "CS0000", "term": "Fall"}


def assignment(key=11, **overrides):
    return {
        "id": key,
        "courseId": "course-a",
        "assignmentName": "Loops",
        "assignmentType": "QC",
        "published": True,
        "dueDate": "2026-09-15 03:59",
        "description": "<p>Read the chapter.</p><p>Complete the questions.</p>",
        "maxPoints": 10,
        "graded": False,
    } | overrides


def test_course_identity_and_optional_section():
    course = parse_course(course_record())
    assert course.external_id == "course-a"
    assert course.section == "CS0000"
    assert course.source_url == DASHBOARD
    assert parse_course(course_record() | {"courseNumber": None}).section is None


@pytest.mark.parametrize("record", [None, {}, course_record() | {"courseId": " "}])
def test_invalid_course_is_not_an_empty_course(record):
    with pytest.raises(ValueError):
        parse_course(record)


def test_published_work_is_kept_while_unpublished_and_grade_aggregates_are_excluded():
    records = [
        assignment(),
        assignment(12, assignmentType="EX"),
        assignment(13, published=False),
        assignment(14, assignmentType="AT"),
        assignment(15, assignmentType="EG"),
    ]
    tasks, warnings, covered, excluded = parse_assignments(
        records, {}, parse_course(course_record())
    )
    assert [task.external_id for task in tasks] == ["11", "12"]
    assert covered and excluded == 3 and warnings
    assert tasks[0].description == "Read the chapter.\nComplete the questions."
    assert tasks[1].url.endswith("/#!/exerciseAssignment/12")


@pytest.mark.parametrize("due", ["2020-01-01 00:00", "2099-01-01 00:00"])
@pytest.mark.parametrize("score", [0, 5, 10])
def test_dates_and_scores_never_establish_student_completion(due, score):
    tasks, _, _, _ = parse_assignments(
        [assignment(dueDate=due, graded=True)], {"11": score}, parse_course(course_record())
    )
    assert tasks[0].submission_status == "unknown"
    assert tasks[0].graded is False
    assert tasks[0].score == score
    assert "graded" in tasks[0].raw_data["unavailable_fields"]
    assert tasks[0].due_at.isoformat() == due.replace(" ", "T") + ":00+00:00"


@pytest.mark.parametrize("due", ["tomorrow", "2026-02-30 03:59", 123])
def test_invalid_deadline_preserves_cached_deadline_and_notes(tmp_path, due):
    db = Database(tmp_path / "db")
    course = parse_course(course_record())
    original, _, _, _ = parse_assignments([assignment()], {}, course)
    db.apply(
        "rephactor",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=original),
        "2026-09-01T00:00:00+00:00",
    )
    db.patch_local(original[0].id, {"note": "Bring notes"})
    changed, warnings, covered, _ = parse_assignments([assignment(dueDate=due)], {}, course)
    assert covered and warnings and changed[0].due_at is None
    assert "due_at" in changed[0].raw_data["unavailable_fields"]
    db.apply(
        "rephactor",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=changed),
        "2026-09-02T00:00:00+00:00",
    )
    cached = db.tasks()[0]
    assert datetime.fromisoformat(cached["due_at"]) == original[0].due_at
    assert cached["local"]["note"] == "Bring notes"


@pytest.mark.parametrize(
    "bad",
    [
        assignment(id=None),
        assignment(id=True),
        assignment(courseId="other"),
        assignment(assignmentName=" "),
        assignment(published="true"),
    ],
)
def test_unread_identity_or_visibility_does_not_cover_list(bad):
    tasks, warnings, covered, _ = parse_assignments([bad], {}, parse_course(course_record()))
    assert not tasks and warnings and not covered


def test_duplicate_identity_is_not_counted_twice_or_treated_as_complete():
    tasks, warnings, covered, _ = parse_assignments(
        [assignment(), assignment()], {}, parse_course(course_record())
    )
    assert len(tasks) == 1 and warnings and not covered


@pytest.mark.parametrize("records", [None, {}, "login"])
def test_unrecognized_assignment_response_is_not_a_true_empty_list(records):
    with pytest.raises(ValueError):
        parse_assignments(records, {}, parse_course(course_record()))


class FakeBrowser:
    interactive = None

    def __init__(self):
        self.contexts = []
        self.reset = AsyncMock()

    def exists(self):
        return True

    @asynccontextmanager
    async def session(self, timezone):
        context = SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
        self.contexts.append(context)
        yield context


class FakeVault:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def connector(tmp_path):
    db = Database(tmp_path / "db")
    db.update_state("rephactor", authorized=True)
    source = RephactorConnector(db, FakeBrowser(), FakeVault())
    source._user = AsyncMock(return_value=("student@example.test", "fingerprint"))
    return source


async def test_sync_reads_active_and_inactive_courses_without_changing_current_course(tmp_path):
    source = connector(tmp_path)
    calls = []

    async def get(context, path, params):
        calls.append((path, params))
        if path == "/user/getUserActiveCourses":
            return [course_record()]
        if path == "/user/getUserInactiveCourses":
            return [course_record("course-b"), course_record()]
        if path == "/course/getAllGradesForStudent":
            return {}
        if path == "/course/getAllAssignmentsInCourse":
            return [assignment(courseId=params["courseId"])]
        pytest.fail(f"Unexpected endpoint: {path}")

    source._get = get
    result = await source.sync()
    assert result.outcome == Outcome.PARTIAL
    assert {course.external_id for course in result.courses} == {"course-a", "course-b"}
    assert {task.course_external_id for task in result.tasks} == {"course-a", "course-b"}
    assert {scope.course_external_id for scope in result.covered_task_scopes} == {
        "course-a",
        "course-b",
    }
    assert len(calls) == 6


async def test_failed_course_assignment_list_preserves_other_course_and_does_not_cover_failure(
    tmp_path,
):
    source = connector(tmp_path)

    async def get(context, path, params):
        if path == "/user/getUserActiveCourses":
            return [course_record(), course_record("course-b")]
        if path == "/user/getUserInactiveCourses":
            raise ValueError("Unrecognized course response")
        if path == "/course/getAllGradesForStudent":
            return {}
        if params["courseId"] == "course-b":
            raise ValueError("Unrecognized assignment response")
        return [assignment()]

    source._get = get
    result = await source.sync()
    assert result.outcome == Outcome.PARTIAL and not result.complete
    assert len(result.tasks) == 1
    assert [scope.course_external_id for scope in result.covered_task_scopes] == ["course-a"]
    assert any("cached tasks retained" in warning for warning in result.warnings)


async def test_unauthenticated_session_never_becomes_successful_empty_sync(tmp_path):
    source = connector(tmp_path)
    source._user.side_effect = TransportError(Outcome.AUTH_REQUIRED, "Reconnect")
    source._get = AsyncMock()
    result = await source.sync()
    assert result.outcome == Outcome.AUTH_REQUIRED
    assert not result.tasks and not result.covered_task_scopes and not result.complete
    source._get.assert_not_called()


async def test_session_expiry_after_first_course_keeps_first_course_tasks(tmp_path):
    source = connector(tmp_path)
    source._get = AsyncMock(
        side_effect=[
            [course_record(), course_record("course-b")],
            [],
            [assignment()],
            {},
            TransportError(Outcome.AUTH_REQUIRED, "Reconnect"),
        ]
    )
    result = await source.sync()
    assert result.outcome == Outcome.PARTIAL
    assert result.metadata["interrupted_by"] == Outcome.AUTH_REQUIRED
    assert len(result.tasks) == 1
    assert [scope.course_external_id for scope in result.covered_task_scopes] == ["course-a"]


async def test_failure_before_any_course_is_never_successful_empty_sync(tmp_path):
    source = connector(tmp_path)
    source._get = AsyncMock(side_effect=TransportError(Outcome.NETWORK_ERROR, "Unavailable"))
    result = await source.sync()
    assert result.outcome == Outcome.NETWORK_ERROR
    assert not result.tasks and not result.covered_task_scopes


async def test_authenticated_true_empty_assignment_list_has_a_covered_scope(tmp_path):
    source = connector(tmp_path)
    source._get = AsyncMock(side_effect=[[course_record()], [], [], {}])
    result = await source.sync()
    assert result.outcome == Outcome.SUCCESS
    assert not result.tasks
    assert [scope.course_external_id for scope in result.covered_task_scopes] == ["course-a"]


async def test_validate_session_rejects_unrecognized_course_response(tmp_path):
    source = connector(tmp_path)
    source._get = AsyncMock(return_value={"login": True})
    assert not await source.validate_session(SimpleNamespace())
    assert "account_fingerprint" not in source.db.state("rephactor")


@pytest.mark.parametrize("kind", [None, [], {}])
def test_malformed_assignment_type_retains_other_rows_without_claiming_coverage(kind):
    tasks, warnings, covered, _ = parse_assignments(
        [assignment(assignmentType=kind), assignment(12)], {}, parse_course(course_record())
    )
    assert [task.external_id for task in tasks] == ["12"]
    assert not covered and any("type" in warning for warning in warnings)


async def test_same_origin_json_without_content_type_is_readable(tmp_path):
    source = connector(tmp_path)
    path = "/course/getAllAssignmentsInCourse"
    response = SimpleNamespace(
        status=200, url=HOME + path, headers={}, json=AsyncMock(return_value=[assignment()])
    )
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    params = {"courseId": "course-a", "ascending": "true"}
    result = await source._get(SimpleNamespace(request=request), path, params)
    assert result == [assignment()]
    request.get.assert_awaited_once_with(HOME + path, params=params, timeout=30000, max_redirects=0)


@pytest.mark.parametrize(
    ("status", "url", "outcome"),
    [
        (200, "https://other.example.test/course/getAllAssignmentsInCourse", Outcome.PARSE_ERROR),
        (200, HOME + "/login", Outcome.PARSE_ERROR),
        (302, HOME + "/course/getAllAssignmentsInCourse", Outcome.PARSE_ERROR),
        (401, HOME + "/course/getAllAssignmentsInCourse", Outcome.AUTH_REQUIRED),
        (429, HOME + "/course/getAllAssignmentsInCourse", Outcome.RATE_LIMITED),
    ],
)
async def test_api_redirects_authentication_and_rate_limits_are_not_parsed_as_lists(
    tmp_path, status, url, outcome
):
    source = connector(tmp_path)
    response = SimpleNamespace(status=status, url=url, headers={}, json=AsyncMock(return_value=[]))
    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    with pytest.raises(TransportError) as error:
        await source._get(context, "/course/getAllAssignmentsInCourse", {})
    assert error.value.outcome == outcome
    response.json.assert_not_called()


async def test_session_cookies_are_saved_restored_to_a_fresh_context_and_refreshed(tmp_path):
    source = connector(tmp_path)
    saved_cookies = [
        {
            "name": "fixture-session",
            "value": "synthetic",
            "domain": "www.rephactor.com",
            "path": "/",
            "expires": -1,
        }
    ]
    login_context = SimpleNamespace(cookies=AsyncMock(return_value=saved_cookies))
    source._get = AsyncMock(return_value=[course_record()])
    assert await source.validate_session(login_context)
    login_context.cookies.assert_awaited_once_with(HOME)
    assert source.vault.get("rephactor_session") == {"cookies": saved_cookies}

    async def user(context):
        assert context is not login_context
        context.add_cookies.assert_awaited_once_with(saved_cookies)
        context.cookies.return_value = [dict(saved_cookies[0], value="refreshed")]
        return "student@example.test", "fingerprint"

    source._user = user
    source._get = AsyncMock(side_effect=[[course_record()], [], [assignment()], {}])
    result = await source.sync()
    assert len(result.tasks) == 1 and result.outcome == Outcome.PARTIAL
    assert source.vault.get("rephactor_session")["cookies"][0]["value"] == "refreshed"
    source.browser.contexts[0].cookies.assert_awaited_once_with(HOME)


async def test_disconnect_clears_saved_session_and_authorization(tmp_path):
    source = connector(tmp_path)
    source.vault.set("rephactor_session", {"cookies": [{"name": "fixture"}]})
    await source.disconnect()
    source.browser.reset.assert_awaited_once()
    assert source.vault.get("rephactor_session") is None
    assert source.db.state("rephactor")["authorized"] is False


async def test_plain_none_current_user_response_requires_login_without_parsing_json(tmp_path):
    source = connector(tmp_path)
    del source._user
    response = SimpleNamespace(
        status=200,
        url=HOME + "/getCurrentUser",
        text=AsyncMock(return_value="none\n"),
        json=AsyncMock(side_effect=ValueError("Not JSON")),
    )
    context = SimpleNamespace(
        pages=[SimpleNamespace(goto=AsyncMock())],
        request=SimpleNamespace(get=AsyncMock(return_value=response)),
    )
    with pytest.raises(TransportError) as error:
        await source._user(context)
    assert error.value.outcome == Outcome.AUTH_REQUIRED
    response.json.assert_not_called()
    assert not await source.validate_session(context)
    assert source.vault.get("rephactor_session") is None
