import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coursedeck.chat import ChatConfigInput, ChatMessageInput, ChatService, build_chat_router
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def seed(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    courses = [Course(provider="test", external_id=key, name=key) for key in ("math", "writing")]
    tasks = [
        Task(
            provider="test",
            external_id="hw",
            course_external_id=course.external_id,
            title="Algebra" if course.external_id == "math" else "Writing secret",
            description="Read chapter one" if course.external_id == "math" else "OTHER COURSE BODY",
            submission_status="submitted",
        )
        for course in courses
    ]
    db.apply(
        "test", SyncResult(outcome=Outcome.SUCCESS, courses=courses, tasks=tasks), now().isoformat()
    )
    return db


def doc(key="material", **overrides):
    return {
        "id": key,
        "provider": "test",
        "course_id": "test:math",
        "title": "Algebra notes",
        "body": "Polynomial factorization examples",
        "kind": "material",
        "url": "https://school.example/content/1",
        "updated_at": "2026-09-10T12:00:00+00:00",
        "complete": True,
    } | overrides


def reply(content="Reply", tool_calls=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls or [],
                    }
                }
            ]
        },
    )


async def configured(tmp_path, handler, material_fetch=None):
    service = ChatService(
        seed(tmp_path),
        None,
        tmp_path,
        material_fetch,
        vault=MemoryVault(),
        transport=httpx.MockTransport(handler),
    )
    await service.save_config(
        ChatConfigInput(
            base_url="https://model.example/v1", model="test-model", api_key="private-api-key"
        )
    )
    return service


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "https://user:secret@model.example",
        "https://model.example?key=secret",
        "file:///tmp/model",
        "http://127.0.0.1.evil.example/v1",
    ],
)
def test_config_rejects_insecure_or_credential_urls(url):
    with pytest.raises(ValueError):
        ChatConfigInput(base_url=url)


async def test_config_keys_are_write_only_and_bound_to_endpoint(tmp_path):
    service = await configured(tmp_path, lambda request: reply())
    assert service.config()["has_api_key"]
    assert "private-api-key" not in json.dumps(service.config())
    assert "private-api-key" not in service.store.path.read_bytes().decode("latin1")
    await service.save_config(
        ChatConfigInput(base_url="https://new-model.example/v1", model="other")
    )
    assert not service.config()["has_api_key"] and not service.vault.values
    await service.save_config(
        ChatConfigInput(base_url="http://127.0.0.1:1234/v1/chat/completions", model="local")
    )
    assert service.config()["base_url"] == "http://127.0.0.1:1234/v1"


async def test_chat_retrieval_scope_citations_and_persistent_history(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return reply("Read the notes [[material]]. Ignore forged reference [[foreign]].")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents(
        [doc(), doc("foreign", course_id="test:writing", body="PRIVATE")]
    )
    result = await service.send(ChatMessageInput(message="Algebra", course_id="test:math"))
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer private-api-key"
    sent = request.content.decode()
    assert "OTHER COURSE BODY" not in sent and "PRIVATE" not in sent
    assert "Polynomial" in sent and "untrusted" in sent
    assert [citation["id"] for citation in result["message"]["citations"]] == ["material"]
    assert any("not be verified" in warning for warning in result["warnings"])
    history = KnowledgeStore(service.store.path).conversation(result["conversation_id"])
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert history["messages"][-1]["citations"] == result["message"]["citations"]
    assert history["messages"][-1]["warnings"] == result["warnings"]


async def test_tools_are_bounded_to_selected_course_and_fetch_once(tmp_path):
    requests, fetches = [], []

    async def fetch(course_id, query):
        fetches.append((course_id, query))
        return {
            "documents": [
                doc("fetched"),
                doc("forbidden", course_id="test:writing", body="SECRET"),
            ],
            "warnings": ["One attachment is unreadable"],
        }

    def call(key, name, **args):
        return {
            "id": key,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return reply(None, [call("a", "fetch_course_materials", query="Algebra")])
        if len(requests) == 2:
            return reply(
                None,
                [
                    call("b", "read_document", document_id="forbidden"),
                    call("c", "fetch_course_materials", query="Algebra"),
                ],
            )
        return reply("See [[fetched]].")

    service = await configured(tmp_path, handler, fetch)
    result = await service.send(
        ChatMessageInput(message="Algebra", course_id="test:math", fetch_materials=True)
    )
    assert fetches == [("test:math", "Algebra")]
    assert service.store.get("forbidden") is None
    assert "SECRET" not in json.dumps(requests)
    assert result["message"]["citations"][0]["id"] == "fetched"
    assert result["warnings"] == ["One attachment is unreadable"]
    tool_outputs = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
    assert json.loads(tool_outputs[1]["content"]) == {"documents": []}


async def test_plain_compatible_endpoint_keeps_explicit_material_fetch(tmp_path):
    requests, fetches = [], []

    async def fetch(course_id, query):
        fetches.append(course_id)
        return {"documents": [doc()], "warnings": []}

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            return httpx.Response(400, json={"error": "Tools unsupported"})
        return reply("Notes [[material]].")

    service = await configured(tmp_path, handler, fetch)
    result = await service.send(
        ChatMessageInput(message="Algebra", course_id="test:math", fetch_materials=True)
    )
    assert fetches == ["test:math"] and len(requests) == 2
    assert "tools" not in requests[-1]
    assert "Polynomial" in json.dumps(requests[-1])
    assert result["warnings"] and result["message"]["citations"]


async def test_model_errors_are_redacted_and_failed_question_is_saved(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": "private-api-key private server response"})

    service = await configured(tmp_path, handler)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await service.send(ChatMessageInput(message="Algebra", course_id="test:math"))
    assert caught.value.status_code == 502
    assert "private" not in str(caught.value.detail)
    history = service.store.conversation(caught.value.detail["conversation_id"])
    assert [message["role"] for message in history["messages"]] == ["user"]


async def test_redirects_do_not_forward_credentials(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/steal"})

    service = await configured(tmp_path, handler)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await service.send(ChatMessageInput(message="Algebra", course_id="test:math"))
    assert len(requests) == 1 and requests[0].url.host == "model.example"


def test_material_library_without_model_and_partial_evidence(tmp_path):
    db = seed(tmp_path)
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [
            doc(body="x" * 6000, complete=False, warnings=["PDF page unreadable"]),
            doc("other", course_id="test:writing"),
        ]
    )
    db.update_state("test", metadata={"materials": {"warnings": ["News page unavailable"]}})
    app = FastAPI()
    app.include_router(build_chat_router(db, None, tmp_path, vault=MemoryVault()))
    client = TestClient(app)
    result = client.get(
        "/api/chat/library", params={"course_id": "test:math", "query": "Algebra"}
    ).json()
    assert "News page unavailable" in result["warnings"]
    assert "PDF page unreadable" in result["warnings"]
    material = next(document for document in result["documents"] if document["id"] == "material")
    assert material["body_truncated"] and len(material["body"]) == 4000
    assert client.get("/api/chat/library/material").json()["body"] == "x" * 6000
    assert client.post("/api/chat/messages", json={"message": "Hi"}).status_code == 400
    assert not store.conversations()


def test_knowledge_upsert_metadata_sanitization_and_backup(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [
            doc(
                url="https://school.example/item?id=7&UserPass=secret&token=secret",
                source_modified_at="old",
                fetched_at="new",
                evidence=["Page 2"],
            )
        ]
    )
    document = store.get("material")
    assert document["url"] == "https://school.example/item?id=7"
    assert document["source_modified_at"] == "old" and document["fetched_at"] == "new"
    assert "metadata" not in document
    store.backup(tmp_path / "backup.sqlite3")
    assert KnowledgeStore(tmp_path / "backup.sqlite3").get("material") == document


def test_task_index_keeps_uncertainty_and_does_not_replace_live_material(tmp_path):
    db = seed(tmp_path)
    db.apply("test", SyncResult(outcome=Outcome.PARTIAL), now().isoformat())
    course = Course(provider="brightspace", external_id="1", name="Math")
    task = Task(
        provider="brightspace",
        course_external_id="1",
        external_id="content-2",
        title="Legacy notes",
        description="Old short summary",
    )
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    rich = doc(
        task.id, provider="brightspace", course_id=course.id, body="New complete extracted PDF"
    )
    store.upsert_documents([rich])
    store.index_tasks(db)
    assert store.get(task.id)["body"] == "New complete extracted PDF"
    assert store.get("task:" + task.id)["kind"] == "assignment"
    material_before = store.get(task.id)
    task.due_at = now()
    task.submission_status = "submitted"
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    store.index_tasks(db)
    scheduled = store.get("task:" + task.id)
    assert scheduled["source_fields"]["due_at"] == task.model_dump(mode="json")["due_at"]
    assert scheduled["source_fields"]["submission_status"] == "submitted"
    assert scheduled["source_fields"]["source_status_known"]
    assert store.get(task.id) == material_before
    task.submission_status = "unknown"
    task.raw_data = {"unavailable_fields": ["submission_status"]}
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    store.index_tasks(db)
    assert not store.get("task:" + task.id)["source_fields"]["source_status_known"]
    assert not store.get("task:" + task.id)["complete"]
    assert store.get(task.id) == material_before
    assert {item["id"] for item in store.search("", [course.id])} == {task.id, "task:" + task.id}
    library = store.library(course_ids=[course.id])
    assert library["total"] == 1 and library["documents"][0]["id"] == task.id
    task_document = store.get("task:test:math:hw")
    assert '"source_availability": "unconfirmed"' in task_document["body"]
    assert '"source_status_known": false' in task_document["body"]
    assert not task_document["complete"] and task_document["warnings"]


def test_legacy_content_seed_stays_separate_from_task_facts(tmp_path):
    db = seed(tmp_path)
    course = Course(provider="brightspace", external_id="1", name="Math")
    task = Task(
        provider="brightspace",
        course_external_id="1",
        external_id="content-2",
        title="Scheduled reading",
        description="Legacy preview",
        due_at=now(),
    )
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.index_tasks(db)
    material = store.get(task.id)
    assert material["kind"] == "material" and not material["complete"]
    assert material["fetched_at"] is None and "source_fields" not in material
    assert store.get("task:" + task.id)["source_fields"]["due_at"]
    store.upsert_documents(
        [
            doc(
                task.id,
                provider="brightspace",
                course_id=course.id,
                body="Complete extracted reading",
            )
        ]
    )
    store.index_tasks(db)
    assert (
        store.get(task.id)["complete"]
        and store.get(task.id)["body"] == "Complete extracted reading"
    )


def test_failed_material_read_retains_old_body_without_claiming_freshness(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents([doc(source_modified_at="original", fetched_at="first-fetch")])
    store.upsert_documents(
        [
            doc(
                body="",
                complete=False,
                source_modified_at="changed",
                updated_at="later",
                checked_at="second-check",
                fetched_at="second-fetch",
                warnings=["Access denied"],
            )
        ]
    )
    cached = store.get("material")
    assert cached["body"] == "Polynomial factorization examples"
    assert cached["source_modified_at"] == "original" and cached["fetched_at"] == "first-fetch"
    assert cached["checked_at"] == "second-check" and not cached["complete"]
    assert "Access denied" in cached["warnings"]


async def test_read_document_paging_reaches_tail_and_preserves_partial_metadata(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return reply(
                None,
                [
                    {
                        "id": "tail",
                        "type": "function",
                        "function": {
                            "name": "read_document",
                            "arguments": json.dumps(
                                {"document_id": "long", "offset": 18000, "limit": 4000}
                            ),
                        },
                    }
                ],
            )
        return reply("The final instructions are recorded [[long]].")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents(
        [
            doc(
                "long",
                body="x" * 20000 + "TAIL EVIDENCE",
                complete=False,
                warnings=["Page 4 unreadable"],
                source_modified_at="source-date",
                fetched_at="fetch-date",
            )
        ]
    )
    result = await service.send(
        ChatMessageInput(message="Explain final instructions", course_id="test:math")
    )
    tool = next(message for message in requests[-1]["messages"] if message["role"] == "tool")
    excerpt = json.loads(tool["content"])["documents"][0]
    assert "TAIL EVIDENCE" in excerpt["body"] and excerpt["body_start"] == 18000
    assert excerpt["total_characters"] == 20013 and excerpt["truncated"]
    assert not excerpt["complete"] and excerpt["source_modified_at"] == "source-date"
    assert "Page 4 unreadable" in result["message"]["warnings"]


async def test_model_max_completion_tokens_compatibility_is_explicit(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "max_tokens" in payload:
            return httpx.Response(
                400, json={"error": {"param": "max_tokens", "message": "unsupported"}}
            )
        return reply("Compatible reply")

    service = await configured(tmp_path, handler)
    result = await service.send(ChatMessageInput(message="Hi", course_id="test:math"))
    assert result["message"]["content"] == "Compatible reply"
    assert len(requests) == 2 and requests[-1]["max_completion_tokens"] == 8192


async def test_existing_conversation_follows_merged_course_scope(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return reply("Done reading")

    service = await configured(tmp_path, handler)
    first = await service.send(ChatMessageInput(message="Writing", course_id="test:writing"))
    service.db.merge_courses("test:math", ["test:writing"])
    await service.send(
        ChatMessageInput(
            message="Explain Algebra and Writing", conversation_id=first["conversation_id"]
        )
    )
    context = requests[-1]["messages"][1]["content"]
    assert "test:math" in context and "test:writing" in context
    assert "OTHER COURSE BODY" in context and "Read chapter one" in context
    app = FastAPI()
    app.include_router(build_chat_router(service.db, None, tmp_path, vault=service.vault))
    client = TestClient(app)
    history = client.get("/api/chat/conversations/" + first["conversation_id"]).json()
    listing = client.get("/api/chat/conversations").json()["conversations"]
    assert history["course_id"] == listing[0]["course_id"] == "test:math"
    assert service.store.conversation(first["conversation_id"])["course_id"] == "test:writing"
    service.db.patch_course("test:math", {"disabled": True})
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await service.send(
            ChatMessageInput(message="Again", conversation_id=first["conversation_id"])
        )


@pytest.mark.parametrize(
    "outcome,metadata,connection,expected",
    [
        (
            "partial",
            {"materials": {"status": "partial", "reason": "timeout"}},
            "connected",
            "timed out",
        ),
        (
            "partial",
            {"materials": {"status": "partial", "reason": "read_error"}},
            "connected",
            "refresh failed",
        ),
        ("auth_required", {}, "connected", "sign-in is required"),
        ("network_error", {}, "connected", "latest sync failed"),
        ("success", {}, "not_connected", "source is disconnected"),
    ],
)
async def test_cached_material_refresh_failures_are_visible_in_library_and_chat(
    tmp_path, outcome, metadata, connection, expected
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return reply("Cached notes [[material]].")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents([doc()])  # Last successful body remains complete in storage.
    service.db.update_state("test", last_outcome=outcome, metadata=metadata)
    connector = SimpleNamespace(display_name="Test source", connection_status=lambda: connection)
    engine = SimpleNamespace(connectors={"test": connector})
    service.engine = engine
    app = FastAPI()
    app.include_router(build_chat_router(service.db, engine, tmp_path, vault=service.vault))
    library = TestClient(app).get("/api/chat/library", params={"course_id": "test:math"}).json()
    assert any(expected in warning for warning in library["warnings"])
    assert service.store.get("material")["complete"]
    result = await service.send(ChatMessageInput(message="Algebra", course_id="test:math"))
    assert any(expected in warning for warning in result["message"]["warnings"])
    assert expected in requests[0]["messages"][1]["content"]
    history = service.store.conversation(result["conversation_id"])
    assert history["messages"][-1]["warnings"] == result["warnings"]


def test_partial_material_warnings_are_not_repeated_as_generic_task_failures(tmp_path):
    service = ChatService(seed(tmp_path), None, tmp_path, vault=MemoryVault())
    service.db.update_state(
        "test",
        last_outcome="partial",
        material_warnings=["One attachment unreadable"],
        metadata={"materials": {"status": "partial", "warnings": ["One attachment unreadable"]}},
    )
    assert service.scope_warnings(["test:math"]) == ["One attachment unreadable"]


async def test_task_context_scopes_related_materials_and_persists_task(tmp_path):
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return reply("Use [[linked]] and [[task:test:math:hw]].")

    service = await configured(tmp_path, handler)
    service.store.upsert_documents(
        [
            doc(
                "linked",
                title="Teacher instructions",
                source_task_id="test:math:hw",
                complete=False,
                warnings=["Attachment unavailable"],
                fetched_at="2026-09-01T12:00:00+00:00",
                checked_at="2026-09-10T12:00:00+00:00",
                source_modified_at="2026-08-30T12:00:00+00:00",
            ),
            doc("keyword"),
            doc("foreign", course_id="test:writing", body="PRIVATEFOREIGN"),
            doc("email", provider="gmail", kind="mail", body="PRIVATEMAIL"),
        ]
    )
    context = service.task_context("test:math:hw")
    assert context["task"]["course_id"] == "test:math"
    assert context["documents"][0]["id"] == "linked"
    assert context["documents"][0]["related_via"] == "association"
    assert {d["id"] for d in context["documents"]} == {"linked", "keyword"}
    assert "description" not in context["task"]
    result = await service.send(
        ChatMessageInput(message="Explain this task", task_id="test:math:hw")
    )
    assert "PRIVATEFOREIGN" not in sent[0] and "PRIVATEMAIL" not in sent[0]
    assert "Read chapter one" in sent[0] and "Attachment unavailable" in sent[0]
    citation = next(c for c in result["message"]["citations"] if c["id"] == "linked")
    assert citation["source_modified_at"] == "2026-08-30T12:00:00+00:00"
    assert citation["checked_at"] != citation["fetched_at"] and not citation["complete"]
    history = service.store.conversation(result["conversation_id"])
    assert history["task_id"] == "test:math:hw"
    await service.send(ChatMessageInput(message="Again", conversation_id=history["id"]))
    assert "Selected task" in sent[-1]
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await service.send(
            ChatMessageInput(
                message="Switch", conversation_id=history["id"], task_id="test:writing:hw"
            )
        )


def test_library_filters_and_task_context_route_without_model(tmp_path):
    db = seed(tmp_path)
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents([doc(kind="announcement", complete=False), doc("complete")])
    app = FastAPI()
    app.include_router(build_chat_router(db, None, tmp_path, vault=MemoryVault()))
    client = TestClient(app)
    response = client.get(
        "/api/chat/library",
        params={
            "provider": "test",
            "kind": "announcement",
            "freshness": "unknown",
            "completeness": "incomplete",
            "course_id": "test:math",
        },
    )
    assert response.status_code == 200 and response.json()["total"] == 1
    assert client.get("/api/chat/library", params={"freshness": "invented"}).status_code == 422
    context = client.get("/api/chat/task-context/test:math:hw")
    assert context.status_code == 200 and context.json()["task"]["title"] == "Algebra"
    assert client.get("/api/chat/task-context/nonexistent").status_code == 404


async def test_unbound_custom_task_does_not_expand_scope_or_include_mail(tmp_path, monkeypatch):
    from coursedeck.mail import CustomTaskInput, MailStore, save_custom_task

    sent = []
    service = await configured(
        tmp_path, lambda request: sent.append(request.content.decode()) or reply()
    )
    task_id = save_custom_task(service.db, CustomTaskInput(title="My private reminder"))
    service.store.upsert_documents([doc(body="COURSE_BODY_MUST_NOT_EXPAND")])

    def forbidden(*args, **kwargs):
        raise AssertionError("Task context must not read mail")

    monkeypatch.setattr(MailStore, "rules", forbidden)
    monkeypatch.setattr(MailStore, "list", forbidden)
    context = service.task_context(task_id)
    assert context["scope"] == [] and context["documents"] == []
    await service.send(ChatMessageInput(message="Help with this", task_id=task_id))
    assert "My private reminder" in sent[0]
    assert "COURSE_BODY_MUST_NOT_EXPAND" not in sent[0]
    assert "Read chapter one" not in sent[0]
