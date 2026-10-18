import asyncio
import json

import httpx
import pytest
from test_chat import configured, doc, reply, seed

from coursedeck.chat import ChatMessageInput, tool_definition
from coursedeck.chat_stream import stream_completion
from coursedeck.chat_tasks import TaskTools
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.task_edits import TaskEdit, TaskEdits
from coursedeck.task_links import public_address, public_body, task_links


def call(name, **arguments):
    return {
        "id": name,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def test_chat_runs_versioned_edit_and_undo_with_persistent_receipts(tmp_path):
    mode = "update"
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        outputs = [
            json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"
        ]
        if not outputs:
            return reply(None, [call("get_task", task_id="test:math:hw")])
        if len(outputs) == 1:
            task = outputs[0]["task"]
            args = {"task_id": task["id"], "expected_version": task["version"]}
            if mode == "update":
                return reply(None, [call("update_task", **args, completion="open")])
            return reply(
                None,
                [
                    call(
                        "undo_change",
                        **args,
                        change_id=outputs[0]["recent_changes"][0]["change_id"],
                    )
                ],
            )
        assert outputs[-1]["changed"]
        return reply("Local change saved [[task:test:math:hw]].")

    service = await configured(tmp_path, handler)
    first = await service.send(ChatMessageInput(message="Mark Algebra open", course_id="test:math"))
    history = service.store.conversation(first["conversation_id"])
    receipts = [item["change"] for item in history["messages"][-1]["activity"] if "change" in item]
    assert receipts[0]["changed"] and receipts[0]["after"]["completion_override"] == "open"
    mode = "undo"
    await service.send(
        ChatMessageInput(message="Undo that change", conversation_id=first["conversation_id"])
    )
    task = next(task for task in service.db.tasks() if task["id"] == "test:math:hw")
    assert task["local"]["completion_override"] is None and task["submission_status"] == "submitted"


async def test_stream_route_holds_recovery_gate_until_body_finishes(tmp_path):
    from fastapi import FastAPI, HTTPException
    from test_chat import MemoryVault

    from coursedeck.chat import build_chat_router
    from coursedeck.maintenance import MaintenanceGate

    started, release = asyncio.Event(), asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({"content": "first"})
            started.set()
            await release.wait()
            yield event(finish="stop")

    service = await configured(tmp_path, lambda _: reply())
    app = FastAPI()
    gate = app.state.maintenance_gate = MaintenanceGate(drain_timeout=0.02)
    app.include_router(
        build_chat_router(
            service.db,
            None,
            tmp_path,
            vault=MemoryVault(),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=Waiting()
                )
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        sending = asyncio.create_task(
            client.post("/api/chat/messages/stream", json={"message": "Algebra"})
        )
        await asyncio.wait_for(started.wait(), 2)
        assert gate.active_requests == 1
        with pytest.raises(HTTPException, match="still running"):
            async with gate.maintenance():
                pytest.fail("Recovery must not proceed during a streamed tool turn")
        release.set()
        response = await sending
        assert response.status_code == 200 and '"type": "done"' in response.text
        assert '"type": "session"' in response.text and '"text": "first"' in response.text
        assert gate.active_requests == 0


def event(delta=None, finish=None):
    return (
        "data: "
        + json.dumps(
            {"choices": [{"delta": delta or {}, "finish_reason": finish}]}, ensure_ascii=False
        )
        + "\n\n"
    ).encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


async def test_stream_is_incremental_and_filters_reasoning(tmp_path):
    released = asyncio.Event()
    first_seen = asyncio.Event()

    class Gated(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({"reasoning_content": "PRIVATE", "content": "<thi"})
            yield event({"content": "nk>HIDDEN</think>你"})
            await released.wait()
            yield event({"content": "好"})
            yield event(finish="stop")
            yield b"data: [DONE]\n\n"

    service = await configured(
        tmp_path,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Gated()
        ),
    )
    events = []

    async def emit(item):
        events.append(item)
        if item.get("text") == "你":
            first_seen.set()

    sending = asyncio.create_task(service.send(ChatMessageInput(message="Algebra"), emit))
    await asyncio.wait_for(first_seen.wait(), 2)
    assert not sending.done(), "The first visible delta must arrive before the provider finishes"
    released.set()
    result = await sending
    assert result["message"]["content"] == "你好"
    assert "HIDDEN" not in json.dumps(events) and "PRIVATE" not in json.dumps(events)
    assert [item["text"] for item in events if item["type"] == "delta"] == ["你", "好"]


async def test_tool_argument_fragments_and_incomplete_stream():
    async def run(chunks):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks)
                )
            )
        ) as client:

            async def emit(_):
                pass

            return await stream_completion(
                client, {"model": "test", "base_url": "https://model.example"}, None, [], [], emit
            )

    chunks = [
        event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-a",
                        "function": {"name": "get_task", "arguments": '{"task_'},
                    }
                ]
            }
        ),
        event({"tool_calls": [{"index": 0, "function": {"arguments": 'id":"test:math:hw"}'}}]}),
    ]
    with pytest.raises(ValueError, match="before completion"):
        await run(chunks)
    result = await run(chunks + [event(finish="tool_calls")])
    assert json.loads(result["tool_calls"][0]["function"]["arguments"])["task_id"] == "test:math:hw"


def test_local_edits_keep_source_facts_survive_sync_and_undo(tmp_path):
    db = seed(tmp_path)
    edits = TaskEdits(db)
    key = "test:math:hw"
    old_version = edits.version(key)
    changed = edits.update(
        TaskEdit(
            task_id=key,
            expected_version=old_version,
            due_at="2026-10-01T23:00:00-04:00",
            completion="open",
        )
    )
    task = next(task for task in db.tasks() if task["id"] == key)
    assert task["source_due_at"] is None and task["due_at"].startswith("2026-10-01")
    assert (
        task["submission_status"] == "submitted" and task["local"]["completion_override"] == "open"
    )
    with pytest.raises(ValueError, match="changed"):
        edits.update(TaskEdit(task_id=key, expected_version=old_version, completion="done"))
    synced = Task(
        provider="test",
        external_id="hw",
        course_external_id="math",
        title="Algebra",
        due_at=now(),
        submission_status="submitted",
    )
    db.apply(
        "test",
        SyncResult(
            outcome=Outcome.PARTIAL,
            courses=[Course(provider="test", external_id="math", name="math")],
            tasks=[synced],
        ),
        now().isoformat(),
    )
    task = next(task for task in db.tasks() if task["id"] == key)
    assert (
        task["due_at"].startswith("2026-10-01")
        and task["source_due_at"] == synced.model_dump(mode="json")["due_at"]
    )
    with pytest.raises(ValueError, match="changed"):
        edits.undo(key, changed["change_id"], changed["version"])
    undone = edits.undo(key, changed["change_id"], edits.version(key))
    assert (
        undone["changed"]
        and not next(task for task in db.tasks() if task["id"] == key)["local"]["due_override"]
    )
    cleared = edits.update(TaskEdit(task_id=key, expected_version=edits.version(key), due_at=None))
    assert next(task for task in db.tasks() if task["id"] == key)["due_at"] is None
    edits.update(
        TaskEdit(
            task_id=key,
            expected_version=cleared["version"],
            restore_due_date=True,
            completion="open",
        )
    )
    db.patch_local(key, {"dismissed": True})
    assert (
        next(task for task in db.tasks() if task["id"] == key)["local"]["completion_override"]
        is None
    )


async def test_task_tool_scope_provenance_and_no_bulk_storage(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    service.store.upsert_documents([doc(url="https://school.example/notes")])
    scope = service.scope("test:math")
    value = ChatMessageInput(message="Find Algebra links", course_id="test:math")
    tools = TaskTools(service, scope, value, lambda documents, **kwargs: documents)
    assert [
        task["id"] for task in (await tools.run("find_tasks", {"query": "Algebra"}))["tasks"]
    ] == ["test:math:hw"]
    with pytest.raises(ValueError, match="outside"):
        await tools.run("get_task", {"task_id": "test:writing:hw"})
    found = await tools.run("get_task", {"task_id": "test:math:hw"})
    with pytest.raises(ValueError, match="link ID"):
        await tools.run(
            "read_task_link", {"task_id": "test:math:hw", "link_id": "http://127.0.0.1"}
        )
    with pytest.raises(ValueError, match="explicit"):
        await tools.run(
            "update_task",
            {
                "task_id": "test:math:hw",
                "expected_version": found["task"]["version"],
                "completion": "done",
            },
        )
    assert "update_task" not in [
        item["function"]["name"] for item in tools.definitions(tool_definition)
    ]

    class Reader:
        calls = 0

        async def read(self, task, link):
            self.calls += 1
            return doc("link:" + link["link_id"], body="ON DEMAND ONLY", kind="linked_document")

    tools.reader = Reader()
    args = {"task_id": "test:math:hw", "link_id": found["links"][0]["link_id"]}
    first = await tools.run("read_task_link", args)
    await tools.run("read_task_link", args)
    assert tools.reader.calls == 1
    assert service.store.get(first["documents"][0]["id"]) is None


async def test_stream_cancel_preserves_text_and_action_receipt(tmp_path):
    waiting = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({"content": "Partial answer"})
            await waiting.wait()

    service = await configured(
        tmp_path,
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Waiting()
        ),
    )
    seen = asyncio.Event()
    events = []

    async def emit(item):
        events.append(item)
        if item["type"] == "delta":
            seen.set()

    pending = asyncio.create_task(service.send(ChatMessageInput(message="Algebra"), emit))
    await asyncio.wait_for(seen.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    conversation = service.store.conversation(events[0]["conversation_id"])
    assert conversation["messages"][-1]["content"] == "Partial answer"
    assert conversation["messages"][-1]["warnings"]
    assert conversation["messages"][-1]["activity"]


async def test_public_reader_rejects_local_addresses_and_credential_links(monkeypatch):
    async def address(*args, **kwargs):
        return [(None, None, None, None, ("127.0.0.1", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", address)
    with pytest.raises(ValueError, match="private"):
        await public_address("private.example")
    for url in (
        "http://example.com/a",
        "https://example.com/a?token=SECRET",
        "https://user:password@example.com/a",
    ):
        with pytest.raises(ValueError):
            await public_body(url)
    links = task_links(
        {
            "id": "t",
            "title": "Test",
            "description": "https://example.com/a?token=SECRET https://docs.google.com/document/d/abc/edit?usp=sharing",
        },
        [],
    )
    assert len(links) == 1 and links[0]["url"] == "https://docs.google.com/document/d/abc/edit"
