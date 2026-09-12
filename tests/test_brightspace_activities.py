import pytest

from coursedeck.connectors.brightspace_activities import (
    BrightspaceActivities,
    apply_quiz_attempts,
    map_quiz,
    object_pages,
    parse_quiz_summary,
)
from coursedeck.connectors.http import TransportError
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, now

COURSE = Course(
    provider="brightspace",
    external_id="1",
    name="Course",
    source_url="https://school.test/d2l/home/1",
)
ENDPOINT = "/d2l/api/le/1.82/1/quizzes/"


def quiz(quiz_id=2, **overrides):
    return {
        "QuizId": quiz_id,
        "Name": "Quiz",
        "IsActive": True,
        "DueDate": None,
        "StartDate": None,
        "EndDate": "2026-09-20T12:00:00Z",
        **overrides,
    }


def attempt(**overrides):
    return {
        "QuizId": 2,
        "UserId": 8,
        "Completed": None,
        "IsPublished": False,
        "Score": None,
        **overrides,
    }


def test_quiz_does_not_invent_due_date_or_store_configuration_secrets():
    task = map_quiz(
        COURSE, quiz(Password="not-a-real-password", NotificationEmail="private@example.test")
    )
    assert task.due_at is None and task.closes_at is not None
    assert "not-a-real-password" not in task.model_dump_json()
    assert "private@example.test" not in task.model_dump_json()
    assert task.submission_status == "unknown"


def test_quiz_attempts_require_completion_not_start_or_grade_configuration():
    task = map_quiz(COURSE, quiz(IsAutoSetGraded=True))
    apply_quiz_attempts(task, [attempt()], "8")
    assert task.submission_status == "open"
    apply_quiz_attempts(task, [attempt(Completed="2026-09-10T12:00:00Z")], "8")
    assert task.submission_status == "submitted"
    apply_quiz_attempts(
        task, [attempt(Completed="2026-09-10T12:00:00Z", IsPublished=True, Score=0)], "8"
    )
    assert task.submission_status == "graded"
    assert task.score is None  # An attempt score is not necessarily the aggregate quiz grade.


@pytest.mark.parametrize("bad", [attempt(UserId=9), attempt(QuizId=3), {"UserId": 8, "QuizId": 2}])
def test_quiz_attempt_identity_and_missing_completion_remain_unknown(bad):
    task = map_quiz(COURSE, quiz())
    with pytest.raises(ValueError):
        apply_quiz_attempts(task, [bad], "8")
    assert task.submission_status == "unknown"


@pytest.mark.asyncio
async def test_quiz_pages_and_attempt_pages_preserve_identity_filter():
    calls = []

    async def get(path, params):
        calls.append((path, params))
        if path == ENDPOINT:
            return {"Objects": [quiz()], "Next": ENDPOINT + "?bookmark=next"}
        if path == ENDPOINT + "?bookmark=next":
            return {"Objects": [quiz(3, IsActive=False)], "Next": None}
        if path == ENDPOINT + "2/attempts/":
            assert params == {"userId": "8"}
            return {"Objects": [attempt()], "Next": ENDPOINT + "2/attempts/?bookmark=next"}
        assert "userId=8" in path
        return {"Objects": [attempt(Completed="2026-09-10T12:00:00Z")], "Next": None}

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(get, "https://school.test", "1.82", "8").quizzes(COURSE, result)
    assert len(calls) == 4
    assert len(result.tasks) == 1
    assert result.tasks[0].submission_status == "submitted"
    assert result.covered_task_scopes[0].external_id_prefix == "quiz-"
    assert not result.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [True, False])
async def test_quiz_permission_denied_is_distinct_from_empty_list(failure):
    async def get(path, params):
        if failure:
            raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 403).")
        return {"Objects": [], "Next": None}

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(get, "https://school.test", "1.82").quizzes(COURSE, result)
    assert bool(result.warnings) is failure
    assert bool(result.covered_task_scopes) is not failure


@pytest.mark.asyncio
async def test_later_quiz_page_failure_keeps_tasks_without_declaring_scope_complete():
    async def get(path, params):
        if path == ENDPOINT:
            return {"Objects": [quiz()], "Next": ENDPOINT + "?bookmark=next"}
        raise TransportError(Outcome.NETWORK_ERROR, "Timed out.")

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(get, "https://school.test", "1.82").quizzes(COURSE, result)
    assert len(result.tasks) == 1
    assert result.tasks[0].submission_status == "unknown"
    assert result.warnings and not result.covered_task_scopes


@pytest.mark.asyncio
async def test_attempt_permission_failure_retains_quiz_and_its_date():
    async def get(path, params):
        if path == ENDPOINT:
            return {"Objects": [quiz(DueDate="2026-09-19T12:00:00Z")], "Next": None}
        raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 403).")

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(get, "https://school.test", "1.82", "8").quizzes(COURSE, result)
    assert result.tasks[0].due_at is not None
    assert result.tasks[0].submission_status == "unknown"
    assert result.warnings and result.covered_task_scopes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link", ["https://other.test/steal", ENDPOINT + "3/attempts/", ENDPOINT + "?userId=9"]
)
async def test_activity_pagination_rejects_endpoint_or_identity_change(link):
    async def get(path, params):
        return {"Objects": [], "Next": link}

    with pytest.raises(TransportError):
        async for _ in object_pages(get, "https://school.test", ENDPOINT, {"userId": "8"}):
            pass


def summary_html(count):
    return (
        '<h2>Quiz Details</h2><h3 class="dhdg_2">Current User</h3><div>Student</div>'
        '<h3 class="dhdg_2">Attempts</h3>'
        f"<div><label>Allowed - 10, Completed - {count}</label></div>"
    )


@pytest.mark.parametrize("count", [0, 1, 9])
def test_quiz_summary_native_completion_count(count):
    summary = parse_quiz_summary(summary_html(count))
    assert summary["known"]
    assert summary["completed_attempts"] == count
    assert summary["submission_status"] == ("submitted" if count else "open")


@pytest.mark.parametrize(
    "html",
    [
        "<p>Attempts Completed. Allowed - 1, Completed - 1</p>",
        '<div class="d2l-htmlblock-untrusted">' + summary_html(1) + "</div>",
        '<h2>Description</h2><h3 class="dhdg_2">Attempts</h3><div>Allowed - 1, Completed - 1</div>',
        summary_html(1) + summary_html(0),
        summary_html("unknown"),
    ],
)
def test_quiz_summary_does_not_trust_instructions_or_ambiguous_structure(html):
    assert not parse_quiz_summary(html)["known"]


@pytest.mark.asyncio
async def test_quiz_api_permission_failure_can_use_verified_summary_callback():
    async def get(path, params):
        if path == ENDPOINT:
            return {"Objects": [quiz()], "Next": None}
        raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 403).")

    async def summary(course, raw):
        assert course.external_id == "1" and raw["QuizId"] == 2
        return parse_quiz_summary(summary_html(1))

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(
        get, "https://school.test", "1.82", "8", quiz_status=summary
    ).quizzes(COURSE, result)
    assert result.tasks[0].submission_status == "submitted"
    assert result.tasks[0].raw_data["submission_summary"]["evidence"] == "quiz_summary"
    assert not result.warnings


def topic(topic_id, due=None, **overrides):
    return {
        "ForumId": 4,
        "TopicId": topic_id,
        "Name": "Discussion",
        "IsHidden": False,
        "DueDate": due,
        "StartDate": None,
        "EndDate": "2026-09-20T12:00:00Z",
        **overrides,
    }


@pytest.mark.asyncio
async def test_discussions_only_due_dates_or_known_tasks_are_emitted():
    async def get(path, params):
        assert "/1.90/" in path
        if path.endswith("/forums/"):
            return [{"ForumId": 4, "IsHidden": False}]
        return [
            topic(1),
            topic(2, "2026-09-19T12:00:00Z"),
            topic(3),
            topic(4, "2026-09-19T12:00:00Z", IsHidden=True),
        ]

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(
        get, "https://school.test", "1.82", known_activity_ids={("1", "discussion-3")}
    ).discussions(COURSE, result)
    assert [task.external_id for task in result.tasks] == ["discussion-2", "discussion-3"]
    assert result.tasks[0].due_at is not None
    assert result.tasks[1].due_at is None
    assert all(task.submission_status == "unknown" for task in result.tasks)
    assert len(result.covered_task_scopes) == 1 and not result.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["forbidden", "missing_due", "empty"])
async def test_discussion_permission_and_missing_due_schema_are_distinct_from_empty(mode):
    async def get(path, params):
        if mode == "forbidden":
            raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 403).")
        if path.endswith("/forums/"):
            return [{"ForumId": 4, "IsHidden": False}]
        if mode == "empty":
            return []
        raw = topic(1)
        del raw["DueDate"]
        return [raw]

    result = SyncResult(outcome=Outcome.PARTIAL)
    await BrightspaceActivities(get, "https://school.test", "1.82").discussions(COURSE, result)
    assert bool(result.covered_task_scopes) is (mode == "empty")
    assert bool(result.warnings) is (mode != "empty")


@pytest.mark.asyncio
async def test_unread_quiz_status_preserves_old_evidence_and_marks_it_unconfirmed(tmp_path):
    db = Database(tmp_path / "quiz.sqlite3")
    previous = map_quiz(COURSE, quiz())
    apply_quiz_attempts(
        previous, [attempt(Completed="2026-09-10T12:00:00Z", IsPublished=True, Score=10)], "8"
    )
    previous.score = 10
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.PARTIAL, courses=[COURSE], tasks=[previous]),
        now().isoformat(),
    )
    db.patch_local(previous.id, {"note": "Retain this note"})

    async def get(path, params):
        if path == ENDPOINT:
            return {"Objects": [quiz()], "Next": None}
        raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 403).")

    result = SyncResult(outcome=Outcome.PARTIAL, courses=[COURSE])
    await BrightspaceActivities(get, "https://school.test", "1.82", "8").quizzes(COURSE, result)
    db.apply("brightspace", result, now().isoformat())
    current = db.tasks()[0]
    assert current["submission_status"] == "graded"
    assert current["graded"] and current["score"] == 10
    assert not current["source_status_known"]
    assert current["local"]["note"] == "Retain this note"
    assert current["missing_count"] == 0

    apply_quiz_attempts(result.tasks[0], [], "8")
    db.apply("brightspace", result, now().isoformat())
    current = db.tasks()[0]
    assert current["submission_status"] == "open" and current["source_status_known"]
