import json
from datetime import UTC, datetime

import pytest
from test_chat import configured, doc, reply
from test_chat_tools import call

from coursedeck.chat import ChatMessageInput
from coursedeck.chat_tasks import TaskTools


@pytest.mark.parametrize("streaming", [False, True])
async def test_last_round_answers_from_evidence_without_requesting_more_tools(tmp_path, streaming):
    requests, events = [], []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) <= 6:
            assert payload.get("tools")
            return reply(
                "正在读取",
                [
                    call(
                        "read_document",
                        document_id="syllabus",
                        offset=6500 + 100 * len(requests),
                        limit=100,
                    )
                ],
            )
        assert "tools" not in payload
        assert "Retrieval has ended" in payload["messages"][-1]["content"]
        assert len([m for m in payload["messages"] if m["role"] == "tool"]) == 6
        return reply("本周考求导；具体当天安排尚未确认。[[syllabus]]")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents([doc("syllabus", title="Syllabus", body="x" * 8000)])

    async def emit(event):
        events.append(event)

    result = await service.send(
        ChatMessageInput(message="看 syllabus，今天考什么？", course_id="test:math"),
        emit if streaming else None,
    )
    assert len(requests) == 7
    assert result["message"]["content"].startswith("本周考求导")
    assert result["message"]["citations"][0]["id"] == "syllabus"
    assert any("tool limit" in warning for warning in result["warnings"])
    if streaming:
        assert events[-1]["type"] == "delta"
        assert events[-1]["text"].startswith("本周考求导")


async def test_repeated_equivalent_reads_run_once_and_then_finish(tmp_path, monkeypatch):
    requests, executions = [], []
    original = TaskTools.run

    async def run(self, name, arguments):
        executions.append(name)
        return await original(self, name, arguments)

    monkeypatch.setattr(TaskTools, "run", run)

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("tools"):
            parameters = {} if len(requests) == 1 else {"offset": 0, "limit": 20}
            return reply(None, [call("find_tasks", query="Algebra", **parameters)])
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        assert outputs[0]["tasks"]
        assert outputs[1]["already_read"] and outputs[2]["already_read"]
        return reply("已找到 Algebra，现有记录没有明确测验范围。")

    service = await configured(tmp_path, handler)
    result = await service.send(ChatMessageInput(message="查 Algebra"))
    assert executions == ["find_tasks"]
    assert len(requests) == 4
    assert "已找到" in result["message"]["content"]
    assert not any("tool limit" in warning for warning in result["warnings"])


async def test_retry_retrieves_original_question_without_authorizing_previous_edit(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        tool_names = {item["function"]["name"] for item in payload["tools"]}
        assert "update_task" not in tool_names and "undo_change" not in tool_names
        if len(requests) == 1:
            return reply(
                None,
                [
                    call(
                        "update_task",
                        task_id="test:math:hw",
                        expected_version="x" * 64,
                        completion="open",
                    )
                ],
            )
        output = json.loads(payload["messages"][-1]["content"])
        assert "unavailable" in output["error"]
        return reply("已重新查阅记录，未再次修改本地状态。")

    service = await configured(tmp_path, handler)
    cid = service.store.create_conversation("Algebra", None)
    service.store.add_message(cid, "user", "Mark Algebra open")
    service.store.add_message(cid, "assistant", "", warnings=["Interrupted"])
    service.store.add_message(cid, "user", "重试")
    service.store.add_message(cid, "assistant", "", warnings=["Interrupted"])
    await service.send(ChatMessageInput(message="重试", conversation_id=cid))
    assert '"query": "Mark Algebra open"' in requests[0]["messages"][1]["content"]
    assert "does not authorize replaying" in requests[0]["messages"][1]["content"]
    task = next(task for task in service.db.tasks() if task["id"] == "test:math:hw")
    assert task["local"]["completion_override"] is None


async def test_retry_selects_subject_evidence_instead_of_latest_unrelated_documents(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return reply("本周求导测验，具体日期仍需核对。[[calculus-quiz]]")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents(
        [
            doc("calculus-quiz", title="Quiz 02", body="This week's quiz is differentiation."),
            doc("syllabus", title="Calculus syllabus", body="Quizzes are on the last class day."),
            doc(
                "unrelated",
                course_id="test:writing",
                title="Writing",
                body="UNRELATED RECENT BODY",
                fetched_at="2026-09-16T12:00:00+00:00",
            ),
        ]
    )
    cid = service.store.create_conversation("Quiz", None)
    service.store.add_message(cid, "user", "我今天数学quiz考什么，看syllabus")
    service.store.add_message(cid, "assistant", "", warnings=["Interrupted"])
    result = await service.send(ChatMessageInput(message="重试", conversation_id=cid))
    context = requests[0]["messages"][1]["content"]
    assert "differentiation" in context and "last class day" in context
    assert "UNRELATED RECENT BODY" not in context
    assert result["message"]["citations"][0]["id"] == "calculus-quiz"


async def test_context_uses_local_date_and_only_relevant_material_warnings(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return reply("依据现有资料回答。[[material]]")

    service = await configured(tmp_path, handler)
    service.db.save_settings(
        service.db.settings().model_copy(update={"timezone": "America/New_York"})
    )
    service.db.update_state(
        "test",
        last_outcome="network_error",
        material_warnings=["Unrelated physics attachment"],
        metadata={"materials": {"warnings": ["Unrelated writing page"]}},
    )
    service.store.upsert_documents(
        [doc(warnings=["Relevant syllabus is incomplete"], complete=False)]
    )
    monkeypatch.setattr("coursedeck.chat.now", lambda: datetime(2026, 9, 16, 2, tzinfo=UTC))
    result = await service.send(ChatMessageInput(message="Algebra 今天", course_id="test:math"))
    context = requests[0]["messages"][1]["content"]
    assert "2026-09-15T22:00:00-04:00" in context
    assert "Unrelated" not in context
    assert any("latest sync failed" in warning for warning in result["warnings"])
    assert "Relevant syllabus is incomplete" in result["warnings"]
    assert "Unrelated physics attachment" in service.scope_warnings(["test:math"])
