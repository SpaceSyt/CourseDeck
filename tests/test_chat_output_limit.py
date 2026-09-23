import json

import httpx
import pytest
from fastapi import HTTPException
from test_chat import configured, reply
from test_chat_tools import Chunks, call, event

from coursedeck.chat import ChatMessageInput
from coursedeck.chat_tasks import TaskTools


@pytest.mark.parametrize("mode", ["buffered", "stream_json", "stream_sse"])
async def test_output_limit_does_not_execute_a_truncated_versioned_edit(
    tmp_path, monkeypatch, mode
):
    requests, executed = [], []
    original = TaskTools.run

    async def run(self, name, arguments):
        executed.append(name)
        return await original(self, name, arguments)

    monkeypatch.setattr(TaskTools, "run", run)

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["max_tokens"] == 8192
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not outputs:
            tool = call("get_task", task_id="test:math:hw")
            finish = "tool_calls"
        else:
            tool = call(
                "update_task",
                task_id="test:math:hw",
                expected_version=outputs[-1]["task"]["version"],
                completion="open",
            )
            finish = "length"
        if mode == "stream_sse":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Chunks(
                    [
                        event({"tool_calls": [{"index": 0, **tool}]}),
                        event(finish=finish),
                    ]
                ),
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {"role": "assistant", "content": None, "tool_calls": [tool]},
                    }
                ]
            },
        )

    service = await configured(tmp_path, handler)

    async def emit(_):
        pass

    with pytest.raises(HTTPException) as error:
        await service.send(
            ChatMessageInput(message="Mark Algebra open"),
            None if mode == "buffered" else emit,
        )
    assert error.value.detail["code"] == "output_limit"
    assert "output limit" in error.value.detail["message"]
    assert len(requests) == 2 and executed == ["get_task"]
    task = next(task for task in service.db.tasks() if task["id"] == "test:math:hw")
    assert task["local"]["completion_override"] is None


async def test_stream_token_parameter_fallback_keeps_the_output_budget(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "max_tokens" in payload:
            return httpx.Response(400, json={"error": {"param": "max_tokens"}})
        assert payload["max_completion_tokens"] == 8192
        return reply("Compatible answer")

    service = await configured(tmp_path, handler)

    async def emit(_):
        pass

    result = await service.send(ChatMessageInput(message="Algebra"), emit)
    assert result["message"]["content"] == "Compatible answer"
    assert len(requests) == 2 and requests[0]["max_tokens"] == 8192
