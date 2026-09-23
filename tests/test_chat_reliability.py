import asyncio
import json
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from test_chat import configured, doc, reply
from test_chat_tools import call

from coursedeck.chat import ChatMessageInput, build_chat_router, local_io
from coursedeck.mail import MailMessage, MailStore


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("finish", ["length", "content_filter"])
async def test_buffered_incomplete_reply_is_not_saved_as_success(tmp_path, streaming, finish):
    service = await configured(
        tmp_path,
        lambda _: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {"role": "assistant", "content": "The deadline is Sep"},
                    }
                ]
            },
        ),
    )
    events = []

    async def emit(event):
        events.append(event)

    with pytest.raises(HTTPException) as error:
        await service.send(ChatMessageInput(message="Algebra"), emit if streaming else None)
    assert error.value.status_code == 502
    conversation = service.store.conversation(service.store.conversations()[0]["id"])
    assert [message["role"] for message in conversation["messages"]] == ["user"]
    assert not any(event["type"] == "delta" for event in events)


async def test_knowledge_search_pages_and_excludes_mail_before_counting(tmp_path):
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not outputs:
            return reply(None, [call("search_knowledge", query="Beta", offset=8, limit=1)])
        seen.append(outputs[-1])
        return reply("Last page [[beta-08]].")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents(
        [doc("alpha", title="Alpha")]
        + [doc(f"beta-{i:02}", title="Beta") for i in range(9)]
        + [doc("private", title="Beta", provider="gmail", kind="email")]
    )
    result = await service.send(ChatMessageInput(message="Alpha", course_id="test:math"))
    assert seen[0]["total"] == 9 and seen[0]["offset"] == 8 and not seen[0]["has_more"]
    assert [item["id"] for item in seen[0]["documents"]] == ["beta-08"]
    assert result["message"]["citations"][0]["id"] == "beta-08"


async def test_evidence_limit_is_visible_to_model_and_saved_reply(tmp_path):
    outputs = []

    def handler(request):
        payload = json.loads(request.content)
        current = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not current:
            return reply(None, [call("search_knowledge", query="Beta")])
        if len(current) == 1:
            return reply(None, [call("read_document", document_id="gamma")])
        outputs.extend(current)
        return reply("Some evidence is unread.")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents(
        [doc(f"alpha-{i}", title="Alpha") for i in range(8)]
        + [doc(f"beta-{i}", title="Beta") for i in range(8)]
        + [doc("gamma", title="Gamma")]
    )
    result = await service.send(ChatMessageInput(message="Alpha", course_id="test:math"))
    assert service.store.get("gamma") is not None
    assert outputs[-1]["documents"] == [] and outputs[-1]["evidence_limit_reached"]
    assert any("evidence limit" in warning for warning in outputs[-1]["warnings"])
    saved = service.store.conversation(result["conversation_id"])["messages"][-1]
    assert any("evidence limit" in warning for warning in saved["warnings"])


async def test_explicit_document_read_returns_body_even_when_search_supplied_it(tmp_path):
    outputs = []

    def handler(request):
        payload = json.loads(request.content)
        current = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if len(current) < 2:
            return reply(
                None,
                [
                    call(
                        "read_document",
                        document_id="long",
                        offset=6500 if current else 0,
                        limit=100,
                    )
                ],
            )
        outputs.extend(current)
        return reply("Read the tail [[long]].")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents([doc("long", title="Alpha", body="x" * 6500 + "TAIL" * 25)])
    await service.send(ChatMessageInput(message="Alpha", course_id="test:math"))
    first, second = [output["documents"][0] for output in outputs]
    assert first["body"] == "x" * 100 and first["already_supplied"]
    assert second["body"] == "TAIL" * 25 and not second["already_supplied"]


async def test_source_material_fetch_is_available_without_toggle_and_still_bounded(tmp_path):
    fetched, outputs = [], []

    async def fetch(course_id, query):
        fetched.append((course_id, query))
        return {"documents": [doc("fresh", title="New notes")], "warnings": []}

    def handler(request):
        payload = json.loads(request.content)
        current = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if len(current) < 2:
            assert "fetch_course_materials" in {t["function"]["name"] for t in payload["tools"]}
            return reply(None, [call("fetch_course_materials", query="New")])
        outputs.extend(current)
        return reply("Fresh material [[fresh]].")

    service = await configured(tmp_path, handler, material_fetch=fetch)
    result = await service.send(ChatMessageInput(message="New notes", course_id="test:math"))
    assert fetched == [("test:math", "New")]
    assert "already fetched" in outputs[-1]["error"]
    assert result["message"]["citations"][0]["id"] == "fresh"


def app_for(service, tmp_path):
    app = FastAPI()
    app.include_router(
        build_chat_router(
            service.db,
            None,
            tmp_path,
            vault=service.vault,
            knowledge_store=service.store,
            transport=service.transport,
        )
    )
    return app


async def test_mail_citation_reads_existing_cache_and_requires_conversation_membership(tmp_path):
    def handler(request):
        payload = json.loads(request.content)
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not outputs:
            return reply(None, [call("search_inbox", query="Survey")])
        if len(outputs) == 1:
            return reply(None, [call("read_inbox_mail", mail_id="one")])
        return reply("Cached mail [[mail:one]].")

    service = await configured(tmp_path, handler)
    mail = MailStore(service.db)
    mail.upsert(
        MailMessage(id="one", sender="Staff", subject="Survey", body="Cached body", unread=True)
    )
    mail.upsert(MailMessage(id="two", sender="Staff", subject="Other", body="Private body"))
    result = await service.send(ChatMessageInput(message="Read email Survey"))
    cid = result["conversation_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service, tmp_path)), base_url="http://localhost"
    ) as client:
        response = await client.get(f"/api/chat/conversations/{cid}/citations/mail:one")
        assert response.status_code == 200
        assert response.json()["body"] == "Cached body" and response.json()["warnings"]
        assert (
            await client.get(f"/api/chat/conversations/{cid}/citations/mail:two")
        ).status_code == 404
        assert (await client.get("/api/chat/library/mail:one")).status_code == 404
    assert service.store.get("mail:one") is None
    assert mail.get("one")["unread"]


async def test_citation_rechecks_current_course_and_temporary_read_errors(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    mail = MailStore(service.db)
    mail.upsert(MailMessage(id="one", sender="Staff", subject="Survey", body="Cached body"))
    cid = service.store.create_conversation("Mail", "test:math")
    service.store.add_message(
        cid,
        "assistant",
        "Citation",
        citations=[
            {"id": "mail:one", "kind": "email", "provider": "gmail"},
            {"id": "browser:one", "kind": "browser_page"},
        ],
    )
    with pytest.raises(HTTPException) as outside:
        service.citation_document(cid, "mail:one")
    assert outside.value.status_code == 404
    with pytest.raises(HTTPException) as temporary:
        service.citation_document(cid, "browser:one")
    assert temporary.value.status_code == 410 and "temporary" in temporary.value.detail

    unscoped = service.store.create_conversation("Material", None)
    service.store.add_message(
        unscoped,
        "assistant",
        "Citation",
        citations=[{"id": "mail:one", "kind": "material", "provider": "test"}],
    )
    with pytest.raises(HTTPException) as material:
        service.citation_document(unscoped, "mail:one")
    assert material.value.status_code == 404


async def test_conversations_generate_concurrently_and_active_deletion_conflicts(tmp_path):
    entered, release = asyncio.Queue(), asyncio.Event()

    async def handler(request):
        await entered.put(json.loads(request.content)["messages"][-1]["content"])
        await release.wait()
        return reply("Finished")

    service = await configured(tmp_path, handler)
    first = service.store.create_conversation("First", None)
    second = service.store.create_conversation("Second", None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service, tmp_path)), base_url="http://localhost"
    ) as client:
        sends = [
            asyncio.create_task(
                client.post(
                    "/api/chat/messages",
                    json={
                        "message": text,
                        "conversation_id": cid,
                    },
                )
            )
            for text, cid in [("First", first), ("Second", second)]
        ]
        try:
            assert {await asyncio.wait_for(entered.get(), 3) for _ in range(2)} == {
                "First",
                "Second",
            }
            assert (await client.delete(f"/api/chat/conversations/{first}")).status_code == 409
            duplicate = await client.post(
                "/api/chat/messages",
                json={
                    "message": "Duplicate",
                    "conversation_id": first,
                },
            )
            assert duplicate.status_code == 409
        finally:
            release.set()
            results = await asyncio.gather(*sends)
        assert all(result.status_code == 200 for result in results)
        service.store.add_message(first, "assistant", "Receipt", activity=[{"id": "tool"}])
        deleted = await client.delete(f"/api/chat/conversations/{first}")
        assert deleted.status_code == 200 and deleted.json() == {"id": first}
        assert (await client.get(f"/api/chat/conversations/{first}")).status_code == 404
        assert (await client.delete(f"/api/chat/conversations/{first}")).status_code == 404
        assert (await client.get(f"/api/chat/conversations/{second}")).status_code == 200
    with service.store.connection() as db:
        assert [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT m.conversation_id FROM chat_activity a "
                "JOIN messages m ON m.id=a.message_id"
            )
        ] == [second]
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


async def test_slow_index_does_not_block_loop_and_cancellation_drains_worker(tmp_path, monkeypatch):
    service = await configured(tmp_path, lambda _: reply())
    started, release = threading.Event(), threading.Event()
    original = service.store.index_tasks
    events = []

    def slow_index(db):
        started.set()
        assert release.wait(5)
        return original(db)

    monkeypatch.setattr(service.store, "index_tasks", slow_index)

    async def emit(event):
        events.append(event)

    pending = asyncio.create_task(service.send(ChatMessageInput(message="Algebra"), emit))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        cid = next(event["conversation_id"] for event in events if event["type"] == "session")
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        with pytest.raises(HTTPException) as active:
            service.delete_conversation(cid)
        assert active.value.status_code == 409
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert service.delete_conversation(cid) == {"id": cid}


async def test_cancelled_local_worker_propagates_without_retrying():
    def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(local_io(cancelled), 1)
