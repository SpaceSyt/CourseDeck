import json

import pytest
from fastapi import HTTPException
from test_chat import configured, reply

from coursedeck.chat import ChatMessageInput
from coursedeck.knowledge import KnowledgeStore


def tool(name, **arguments):
    return {
        "id": name,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def test_file_only_message_citations_followup_and_restart(tmp_path):
    requests = []
    file_id = ""

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "EXPLICIT UPLOADED FACT" in json.dumps(payload["messages"])
        return reply(f"The uploaded fact. [[{file_id}]]")

    service = await configured(tmp_path, handler)
    uploaded = service.attachments.upload("notes.md", b"EXPLICIT UPLOADED FACT")
    file_id = uploaded["id"]
    events = []

    async def emit(event):
        events.append(event)

    result = await service.send(ChatMessageInput(attachment_ids=[file_id]), emit)
    cid = result["conversation_id"]
    assert result["message"]["citations"][0]["id"] == file_id
    assert result["message"]["citations"][0]["kind"] == "uploaded_file"
    assert events[0]["type"] == "session"
    assert events[0]["message"]["attachments"][0]["name"] == "notes.md"
    document = service.citation_document(cid, file_id)
    assert document["body"] == "EXPLICIT UPLOADED FACT"
    await service.send(ChatMessageInput(message="Explain that further", conversation_id=cid))
    assert len(requests) == 2
    store = KnowledgeStore(service.store.path)
    assert store.conversation(cid)["messages"][0]["attachments"][0]["id"] == file_id
    assert store.search("EXPLICIT UPLOADED FACT", ["test:math"]) == []
    store.close()
    service.delete_conversation(cid)
    with pytest.raises(HTTPException):
        service.attachments.document(file_id, cid)


async def test_other_conversation_cannot_read_or_attach_file(tmp_path):
    file_id = ""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert "PRIVATE UPLOAD BODY" not in json.dumps(payload)
        if calls == 1:
            return reply(tool_calls=[tool("read_document", document_id=file_id)])
        output = json.loads(payload["messages"][-1]["content"])
        assert output == {"documents": []}
        return reply("The file is unavailable in this chat.")

    service = await configured(tmp_path, handler)
    uploaded = service.attachments.upload("private.txt", b"PRIVATE UPLOAD BODY")
    file_id = uploaded["id"]
    owner = service.store.create_conversation("Owner", None)
    service.store.add_message(owner, "user", "Read", attachment_ids=[file_id])
    result = await service.send(ChatMessageInput(message="Check a file"))
    with pytest.raises(HTTPException):
        await service.send(
            ChatMessageInput(
                message="Read", conversation_id=result["conversation_id"], attachment_ids=[file_id]
            )
        )
    assert len(service.store.conversation(result["conversation_id"])["messages"]) == 2
    assert calls == 2


async def test_uploaded_text_search_and_explicit_read_beyond_initial_excerpt(tmp_path):
    calls = 0
    file_id = ""
    body = "Introduction\n" + "filler " * 2200 + "\nZEBRA_TARGET: 47\nFinal section."

    def handler(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            assert "ZEBRA_TARGET: 47" not in json.dumps(payload["messages"])
            return reply(tool_calls=[tool("search_uploaded_files", query="ZEBRA_TARGET")])
        output = json.loads(payload["messages"][-1]["content"])
        assert "ZEBRA_TARGET: 47" in output["documents"][0]["body"]
        if calls == 2:
            assert output["total"] == 1
            return reply(
                tool_calls=[
                    tool("read_document", document_id=file_id, offset=body.index("ZEBRA_TARGET"))
                ]
            )
        assert output["documents"][0]["body_start"] == body.index("ZEBRA_TARGET")
        return reply(f"47 [[{file_id}]]")

    service = await configured(tmp_path, handler)
    file_id = service.attachments.upload("long.txt", body.encode())["id"]
    result = await service.send(
        ChatMessageInput(message="Summarize attachment", attachment_ids=[file_id])
    )
    assert result["message"]["citations"][0]["id"] == file_id
    assert calls == 3


async def test_invalid_attachments_do_not_create_messages_or_conversations(tmp_path):
    service = await configured(tmp_path, lambda request: pytest.fail("Must not call the model"))
    before = service.store.conversations()
    with pytest.raises(HTTPException):
        await service.send(ChatMessageInput(message="Read", attachment_ids=["a" * 32]))
    assert service.store.conversations() == before
    cid = service.store.create_conversation("Atomic", None)
    uploaded = service.attachments.upload("file.txt", b"Valid text")
    with pytest.raises(HTTPException):
        service.store.add_message(cid, "user", "Read", attachment_ids=[uploaded["id"], "b" * 32])
    assert service.store.conversation(cid)["messages"] == []
    service.attachments.delete_draft(uploaded["id"])


@pytest.mark.parametrize(
    "value",
    [{"message": " "}, {"attachment_ids": ["../file"]}, {"attachment_ids": ["a" * 32] * 6}],
)
def test_invalid_upload_message_schema(value):
    with pytest.raises(ValueError):
        ChatMessageInput(**value)
