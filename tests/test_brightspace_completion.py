import pytest

from coursedeck.connectors.brightspace import BrightspaceAPITransport, map_scheduled_item
from coursedeck.domain import Course, Outcome, SyncResult


def content(**changes):
    return {
        "OrgUnitId": "1",
        "ItemId": 10,
        "ItemName": "Reading fixture",
        "ItemType": 1,
        "ActivityType": 1,
        "CompletionType": 2,
        "DateCompleted": None,
        "DueDate": "2026-09-18T03:59:00Z",
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "open"),
        ({"DateCompleted": "2026-09-17T12:00:00Z"}, "completed"),
        ({"CompletionType": 1, "DateCompleted": "2026-09-17T12:00:00Z"}, "completed"),
        ({"ActivityType": 7, "DateCompleted": "2026-09-17T12:00:00Z"}, "unknown"),
        ({"ActivityType": 3, "DateCompleted": "2026-09-17T12:00:00Z"}, "unknown"),
        ({"CompletionType": 3}, "unknown"),
        ({"DateCompleted": "invalid"}, "unknown"),
    ],
)
def test_content_completion_is_not_assignment_submission(changes, expected):
    course = Course(
        provider="brightspace",
        external_id="1",
        name="Fixture",
        source_url="https://school.example/d2l/home/1",
    )
    task = map_scheduled_item(course, content(**changes))
    assert task.submission_status == expected
    assert not task.graded


@pytest.mark.parametrize("malformed", [False, True])
async def test_completed_and_deadline_removed_content_is_refreshed(malformed):
    async def get(path, params):
        if "myenrollments" in path:
            return {
                "Items": [{"OrgUnit": {"Id": 1, "Name": "Fixture"}}],
                "PagingInfo": {"HasMoreItems": False},
            }
        if "myItems" in path:
            assert params == {"completion": 1}
            objects = [content(DueDate=None, DateCompleted="2026-09-17T12:00:00Z")]
            if malformed:
                objects.append({"ItemName": "Unidentified record"})
            return {"Objects": objects, "Next": None}
        return []

    transport = BrightspaceAPITransport(
        get, "https://school.example", "1.49", "1.82", {("1", "content-10")}
    )
    result = SyncResult(outcome=Outcome.PARTIAL, metadata={"content_items_checked": 0})
    await transport.scheduled_content(
        Course(
            provider="brightspace",
            external_id="1",
            name="Fixture",
            source_url="https://school.example/d2l/home/1",
        ),
        result,
    )
    assert len(result.tasks) == 1
    assert result.tasks[0].due_at is None
    assert result.tasks[0].submission_status == "completed"
    scopes = {s.external_id_prefix for s in result.covered_task_scopes}
    assert ("content-" in scopes) is not malformed


async def test_unrecognized_submission_payload_does_not_drop_assignment():
    async def get(path, params):
        if "myenrollments" in path:
            return {
                "Items": [{"OrgUnit": {"Id": 1, "Name": "Fixture"}}],
                "PagingInfo": {"HasMoreItems": False},
            }
        if "myItems" in path:
            return {"Objects": [], "Next": None}
        if "mysubmissions" in path:
            return {"unexpected": "schema"}
        return [{"Id": 2, "Name": "Assignment", "DueDate": "2026-09-18T03:59:00Z"}]

    result = await BrightspaceAPITransport(get, "https://school.example", "1.49", "1.82").sync()
    assert len(result.tasks) == 1
    assert result.tasks[0].submission_status == "unknown"
    assert result.tasks[0].due_at is not None
    assert any("Submission schema" in w for w in result.warnings)


async def test_normal_sync_keeps_explicit_content_deadlines_but_not_ordinary_materials():
    calls = []

    async def get(path, params):
        calls.append(path)
        if "myenrollments" in path:
            return {
                "Items": [{"OrgUnit": {"Id": 1, "Name": "Course"}}],
                "PagingInfo": {"HasMoreItems": False},
            }
        if "myItems" in path:
            assert params == {"completion": 1}
            return {
                "Objects": [
                    content(ItemId=10),
                    content(ItemId=11, DueDate=None),
                    content(ItemId=12, DueDate=None, DateCompleted="2026-09-17T12:00:00Z"),
                ],
                "Next": None,
            }
        if path.endswith("/quizzes/"):
            return {"Objects": [], "Next": None}
        return []

    result = await BrightspaceAPITransport(
        get, "https://school.example", "1.49", "1.82", {("1", "content-12")}
    ).sync()
    assert {task.external_id for task in result.tasks} == {"content-10", "content-12"}
    assert result.tasks[0].due_at is not None
    assert result.tasks[1].due_at is None and result.tasks[1].submission_status == "completed"
    assert any(scope.external_id_prefix == "content-" for scope in result.covered_task_scopes)
    assert all("description" in task.raw_data["unavailable_fields"] for task in result.tasks)
    assert not any("/content/topics/" in path or "/content/enforced/" in path for path in calls)
    assert result.outcome == Outcome.SUCCESS
