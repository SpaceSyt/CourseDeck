import pytest
from test_chat import configured, reply, seed

from coursedeck.chat import ChatMessageInput, tool_definition
from coursedeck.chat_tasks import TaskTools
from coursedeck.task_edits import TaskEdits
from coursedeck.task_permissions import parse_intent


@pytest.mark.parametrize(
    "message",
    [
        "Do not mark Algebra done",
        "Why did the deadline change?",
        "不要修改作业",
        "为什么要修改截止日期",
        "> Mark Algebra done",
        '"Mark Algebra done"',
        "Explain how to mark Algebra done",
        "If I mark Algebra done",
        "Could you explain this update?",
    ],
)
def test_discussion_negation_and_quotes_do_not_authorize_writes(message):
    assert parse_intent(message) is None


@pytest.mark.parametrize(
    "message,field,value",
    [
        ("Mark Algebra open", "completion", "open"),
        ("Please mark this task done", "completion", "done"),
        ("把作业标记为未完成", "completion", "open"),
        ("Set Algebra deadline to 2026-09-12 18:00", "due_at", "2026-09-12T18:00:00-04:00"),
        ("把作业ddl改到2026-09-12T18:00:00-04:00", "due_at", "2026-09-12T18:00:00-04:00"),
    ],
)
def test_explicit_commands_bind_values(message, field, value):
    assert parse_intent(message, "America/New_York").fields == {field: value}


def test_ambiguous_or_nonexistent_local_deadlines_require_offsets():
    assert parse_intent("Set this deadline to 2026-11-01 01:30", "America/New_York") is None
    assert parse_intent("Set this deadline to 2026-03-08 02:30", "America/New_York") is None
    assert parse_intent("Set this deadline to tomorrow", "America/New_York") is None


def test_note_values_are_literal_data_and_keep_punctuation():
    assert parse_intent('Set Algebra note to "Do not forget. Why?"').fields == {
        "note": "Do not forget. Why?"
    }
    assert parse_intent("把Algebra的备注改为复习，不要忘记！").fields == {
        "note": "复习，不要忘记！"
    }
    assert parse_intent("不要把Algebra的备注改为空") is None
    assert parse_intent("请标记Algebra为完成").fields == {"completion": "done"}


def test_relative_deadline_needs_explicit_clock_and_uses_local_day(monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(
        "coursedeck.task_permissions.now",
        lambda: datetime.fromisoformat("2026-09-12T02:00:00+00:00"),
    )
    for command in [
        "Reschedule Algebra to tomorrow at 6 pm",
        "把Algebra的ddl推迟到明天下午6:00",
    ]:
        assert parse_intent(command, "America/New_York").fields == {
            "due_at": "2026-09-12T18:00:00-04:00"
        }
    assert parse_intent("Reschedule Algebra to tomorrow afternoon") is None


async def test_model_cannot_expand_granted_target_fields_values_or_action(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    value = ChatMessageInput(message="Mark Algebra open", course_id="test:math")
    tools = TaskTools(
        service, service.scope(value.course_id), value, lambda values, **kwargs: values
    )
    task = (await tools.run("get_task", {"task_id": "test:math:hw"}))["task"]
    args = {"task_id": task["id"], "expected_version": task["version"], "completion": "open"}
    for patch in [{"completion": "done"}, {"note": "Injected note"}, {"due_at": None}]:
        with pytest.raises(ValueError, match="fields or values"):
            await tools.run("update_task", args | patch)
    with pytest.raises(ValueError):
        await tools.run("undo_change", args | {"change_id": "invented"})
    functions = [item["function"] for item in tools.definitions(tool_definition)]
    assert not any(item["name"] == "undo_change" for item in functions)
    edit = next(item for item in functions if item["name"] == "update_task")
    assert set(edit["parameters"]["properties"]) == {"task_id", "expected_version", "completion"}
    assert edit["parameters"]["properties"]["completion"]["enum"] == ["open"]
    read = next(item for item in functions if item["name"] == "get_task")
    assert "enum" not in read["parameters"]["properties"]["task_id"]
    result = await tools.run("update_task", args)
    assert result["changed"] and result["after"]["completion_override"] == "open"


async def test_task_disabled_after_read_cannot_be_modified_even_when_selected(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    value = ChatMessageInput(
        message="Mark this done", task_id="test:math:hw", course_id="test:math"
    )
    tools = TaskTools(
        service, service.scope(value.course_id), value, lambda values, **kwargs: values
    )
    task = (await tools.run("get_task", {"task_id": value.task_id}))["task"]
    service.db.patch_course("test:math", {"disabled": True})
    with pytest.raises(ValueError, match="outside"):
        await tools.run(
            "update_task",
            {"task_id": value.task_id, "expected_version": task["version"], "completion": "done"},
        )


def test_ui_and_ai_share_atomic_history_conflicts_and_undo(tmp_path):
    db = seed(tmp_path)
    edits = TaskEdits(db)
    key = "test:math:hw"
    original = edits.version(key)
    db.patch_local(key, {"note": "Local UI note"})
    assert edits.recent(key)
    with pytest.raises(ValueError, match="changed"):
        edits.patch_local(key, {"pinned": True}, expected_version=original)
    # An unversioned UI field patch merges the latest other fields within one write transaction.
    db.patch_local(key, {"pinned": True})
    assert next(t for t in db.tasks() if t["id"] == key)["local"]["note"] == "Local UI note"
    history = edits.recent(key)
    edits.undo(key, history[0]["change_id"], edits.version(key))
    current = next(t for t in db.tasks() if t["id"] == key)
    assert not current["local"]["pinned"] and current["local"]["note"] == "Local UI note"
    before = len(edits.recent(key))
    db.patch_local(key, {"note": "Local UI note"})
    assert len(edits.recent(key)) == before


async def test_write_transaction_rechecks_course_after_preliminary_validation(
    tmp_path, monkeypatch
):
    service = await configured(tmp_path, lambda _: reply())
    value = ChatMessageInput(message="Mark Algebra open", course_id="test:math")
    tools = TaskTools(service, service.scope(value.course_id), value, lambda docs, **kwargs: docs)
    task = (await tools.run("get_task", {"task_id": "test:math:hw"}))["task"]
    original = tools.edits.update

    def disable_then_update(edit, **kwargs):
        service.db.patch_course("test:math", {"disabled": True})
        return original(edit, **kwargs)

    monkeypatch.setattr(tools.edits, "update", disable_then_update)
    with pytest.raises(ValueError, match="outside"):
        await tools.run(
            "update_task",
            {
                "task_id": task["id"],
                "expected_version": task["version"],
                "completion": "open",
            },
        )
    assert not tools.edits.recent(task["id"])


async def test_stable_read_does_not_attach_new_version_to_old_facts(tmp_path, monkeypatch):
    service = await configured(tmp_path, lambda _: reply())
    value = ChatMessageInput(message="Mark Algebra open", course_id="test:math")
    tools = TaskTools(service, service.scope(value.course_id), value, lambda docs, **kwargs: docs)
    get = tools.get
    calls = 0

    def concurrently_updated(task_id):
        nonlocal calls
        calls += 1
        task = get(task_id)
        if calls == 2:
            service.db.patch_local(task_id, {"note": "Concurrent UI note"})
        return task

    monkeypatch.setattr(tools, "get", concurrently_updated)
    task = (await tools.run("get_task", {"task_id": "test:math:hw"}))["task"]
    assert task["local"]["note"] == "Concurrent UI note"
    assert task["version"] == tools.edits.version(task["id"])


async def test_new_duplicate_name_revokes_existing_target_grant(tmp_path):
    from coursedeck.domain import Course, Outcome, SyncResult, Task, now

    service = await configured(tmp_path, lambda _: reply())
    value = ChatMessageInput(message="Mark Algebra open", course_id="test:math")
    tools = TaskTools(service, service.scope(value.course_id), value, lambda docs, **kwargs: docs)
    task = (await tools.run("get_task", {"task_id": "test:math:hw"}))["task"]
    service.db.apply(
        "test",
        SyncResult(
            outcome=Outcome.PARTIAL,
            courses=[Course(provider="test", external_id="math", name="math")],
            tasks=[
                Task(
                    provider="test", external_id="other", course_external_id="math", title="Algebra"
                )
            ],
        ),
        now().isoformat(),
    )
    with pytest.raises(ValueError, match="matching changed"):
        await tools.run(
            "update_task",
            {
                "task_id": task["id"],
                "expected_version": task["version"],
                "completion": "open",
            },
        )
    assert not tools.edits.recent(task["id"])


def test_source_verification_change_invalidates_version_without_changing_payload(tmp_path):
    from coursedeck.task_edits import TaskEdit

    db = seed(tmp_path)
    edits = TaskEdits(db)
    key = "test:math:hw"
    version = edits.version(key)
    with db.connection() as conn:
        conn.execute("UPDATE tasks SET missing_count=1 WHERE id=?", (key,))
    assert edits.version(key) != version
    with pytest.raises(ValueError, match="changed"):
        edits.update(TaskEdit(task_id=key, expected_version=version, completion="open"))
