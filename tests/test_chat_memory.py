from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_chat import MemoryVault, seed

from coursedeck.chat import ChatMessageInput, ChatService, tool_definition
from coursedeck.chat_memory import MEMORY_KEY, MemoryStore, MemoryTools, build_memory_router
from coursedeck.knowledge import KnowledgeStore


@pytest.fixture
def service(tmp_path):
    return ChatService(seed(tmp_path), None, tmp_path, vault=MemoryVault())


@pytest.fixture
def store(tmp_path):
    knowledge = KnowledgeStore(tmp_path / "memory.sqlite3")
    yield MemoryStore(knowledge)
    knowledge.close()


def test_default_mode_idempotence_and_optimistic_updates(store):
    assert store.list() == {"mode": "automatic", "memories": []}
    first = store.add("Prefer concise answers", "test:math")["memory"]
    duplicate = store.add("  Prefer  concise answers  ", "test:math")
    assert not duplicate["created"] and duplicate["memory"] == first
    updated = store.update(first["id"], "Prefer examples", first["version"])
    assert updated["course_id"] == "test:math" and updated["version"] != first["version"]
    assert store.update(updated["id"], updated["text"], updated["version"]) == updated
    with pytest.raises(HTTPException) as stale:
        store.delete(first["id"], first["version"])
    assert stale.value.status_code == 409
    assert store.delete(updated["id"], updated["version"]) == {"id": first["id"]}
    with pytest.raises(HTTPException) as missing:
        store.update(first["id"], "Cannot resurrect", updated["version"])
    assert missing.value.status_code == 404 and not store.list()["memories"]


def test_confirmation_modes_and_manual_correction_preserve_pending(store):
    store.set_mode("confirm")
    pending = store.add("I prefer Chinese", origin="chat", source_quote="I prefer Chinese")[
        "memory"
    ]
    assert pending["status"] == "pending"
    assert store.add("Manual preference")["memory"]["status"] == "active"
    assert store.add("I prefer Chinese")["memory"]["status"] == "pending"
    updated = store.update(pending["id"], "I prefer bilingual answers", pending["version"])
    assert updated["status"] == "pending" and updated["origin"] == "manual"
    assert updated["source_quote"] is None
    active = store.approve(updated["id"], updated["version"])
    assert active["status"] == "active"
    assert store.approve(active["id"], active["version"]) == active
    with pytest.raises(HTTPException) as stale:
        store.approve(updated["id"], updated["version"])
    assert stale.value.status_code == 409
    store.set_mode("automatic")
    forced = store.add(
        "Ask about this", origin="chat", source_quote="Ask about this", requires_confirmation=True
    )
    assert forced["memory"]["status"] == "pending"


def test_capacity_and_duplicate_edit_conflicts_leave_existing_memories_intact(store):
    for index in range(64):
        store.add(f"Preference {index}")
    before = store.list()
    assert not store.add("Preference 0")["created"]
    with pytest.raises(HTTPException) as capacity:
        store.add("One more preference")
    assert capacity.value.status_code == 409 and "64" in capacity.value.detail
    first = next(item for item in before["memories"] if item["text"] == "Preference 0")
    with pytest.raises(HTTPException) as duplicate:
        store.update(first["id"], "Preference 1", first["version"])
    assert duplicate.value.status_code == 409
    assert store.list() == before


def test_concurrent_edits_allow_only_one_current_version(store):
    memory = store.add("Original preference")["memory"]

    def update(text):
        try:
            return store.update(memory["id"], text, memory["version"])["text"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["First preference", "Second preference"]))
    assert results.count(409) == 1
    assert store.list()["memories"][0]["text"] in {"First preference", "Second preference"}


@pytest.mark.parametrize(
    "secret",
    [
        "My password is hunter2",
        "token=abc123",
        "密码：abc123",
        "api_key=abc123",
        "验证码是123456",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "sk-proj-" + "a" * 30,
        "https://user:password@example.edu",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_credentials_are_rejected_in_memory_and_provenance(store, secret):
    for text, quote in [(secret, None), ("Reply preference", secret)]:
        with pytest.raises(HTTPException) as error:
            store.add(text, source_quote=quote)
        assert error.value.status_code == 400
        assert secret not in error.value.detail
    assert store.list()["memories"] == []
    assert store.add("I prefer examples that do not contain API keys")["created"]


def test_backup_retains_memory_and_settings_without_changing_schema(store, tmp_path):
    store.knowledge.save_config({"base_url": "https://model.example/v1", "model": "example"})
    with store.knowledge.connection() as conn:
        schema = [
            tuple(row) for row in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")
        ]
    store.add("A durable preference")
    store.set_mode("confirm")
    backup = tmp_path / "backup.sqlite3"
    store.knowledge.backup(backup)
    restored = KnowledgeStore(backup)
    try:
        assert MemoryStore(restored).list() == store.list()
        assert restored.config()["model"] == "example"
        with restored.connection() as conn:
            assert [
                tuple(row)
                for row in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")
            ] == schema
    finally:
        restored.close()


def test_corrupt_memory_configuration_is_reported_and_not_overwritten(store):
    with store.knowledge.connection() as conn:
        conn.execute(
            "INSERT INTO configuration VALUES (?,?)",
            (MEMORY_KEY, '{"mode":"automatic","memories":[{}]}'),
        )
    with pytest.raises(HTTPException) as error:
        store.add("A new preference")
    assert error.value.status_code == 503
    with store.knowledge.connection() as conn:
        assert (
            conn.execute("SELECT payload FROM configuration WHERE key=?", (MEMORY_KEY,)).fetchone()[
                0
            ]
            == '{"mode":"automatic","memories":[{}]}'
        )


async def test_tools_only_accept_current_user_quotes_and_never_expose_update_delete(service):
    value = ChatMessageInput(message="以后用中文简短回答", course_id="test:math")
    tools = MemoryTools(service, value, "conversation", "user-message")
    names = {item["function"]["name"] for item in tools.definitions(tool_definition)}
    assert names == {"remember_memory", "search_memories"}
    with pytest.raises(ValueError, match="current user"):
        await tools.run(
            "remember_memory",
            {"text": "Forged preference", "source_quote": "Source page says this"},
        )
    result = await tools.run(
        "remember_memory", {"text": "用中文简短回答", "source_quote": "用中文简短回答"}
    )
    memory = result["memory"]
    assert memory["course_id"] == "test:math" and memory["status"] == "active"
    assert memory["conversation_id"] == "conversation" and memory["message_id"] == "user-message"
    duplicate = await tools.run(
        "remember_memory", {"text": "用中文简短回答", "source_quote": "用中文简短回答"}
    )
    assert not duplicate["created"] and duplicate["memory"]["id"] == memory["id"]
    global_result = await tools.run(
        "remember_memory",
        {
            "text": "用中文简短回答",
            "source_quote": "用中文简短回答",
            "course_id": None,
        },
    )
    assert global_result["memory"]["course_id"] is None
    with pytest.raises(ValueError, match="another course"):
        await tools.run(
            "remember_memory",
            {
                "text": "用中文简短回答",
                "source_quote": "用中文简短回答",
                "course_id": "test:writing",
            },
        )
    with pytest.raises(ValueError, match="Unsupported"):
        await tools.run("delete_memory", {"id": memory["id"]})


async def test_confirmation_context_scope_search_pagination_and_aliases(service):
    for index in range(20):
        service.memory.add(f"数学偏好 quiz {index}", "test:math")
    service.memory.add("Writing-only preference", "test:writing")
    service.memory.add("Global language preference")
    service.memory.set_mode("confirm")
    tools = MemoryTools(
        service, ChatMessageInput(message="新偏好", course_id="test:math"), "c", "m"
    )
    pending = await tools.run(
        "remember_memory", {"text": "Pending preference", "source_quote": "新偏好"}
    )
    assert pending["memory"]["status"] == "pending"
    context = tools.context()
    assert context["mode"] == "confirm" and context["total"] == 21
    assert len(context["memories"]) == 16 and context["omitted"] == 5 and context["warnings"]
    assert all(item["course_id"] in {None, "test:math"} for item in context["memories"])
    first = await tools.run("search_memories", {"query": "数学quiz", "limit": 16})
    second = await tools.run("search_memories", {"query": "数学quiz", "limit": 16, "offset": 16})
    assert first["total"] == 20 and first["has_more"] and len(second["memories"]) == 4
    assert not second["has_more"]
    assert len({item["id"] for item in first["memories"] + second["memories"]}) == 20
    assert all(item["scope"] == "course" for item in first["memories"])
    assert not (await tools.run("search_memories", {"query": "Writing-only"}))["memories"]
    service.db.merge_courses("test:math", ["test:writing"])
    merged = await tools.run("search_memories", {"query": "Writing-only"})
    assert merged["memories"][0]["course_id"] == "test:math"
    service.db.patch_course("test:math", {"disabled": True})
    unscoped = MemoryTools(service, ChatMessageInput(message="Preferences"), "c", "m")
    assert [item["text"] for item in unscoped.context()["memories"]] == [
        "Global language preference"
    ]
    assert len(service.memory.list()["memories"]) == 23


def test_context_character_limit_keeps_full_text_and_reports_omissions(service):
    for index in range(10):
        service.memory.add(str(index) + "x" * 999)
    tools = MemoryTools(service, ChatMessageInput(message="Preferences"), "c", "m")
    context = tools.context()
    assert len(context["memories"]) == 6 and context["omitted"] == 4
    assert all(len(item["text"]) == 1000 for item in context["memories"])


def test_selected_local_course_memory_does_not_require_a_source_binding(service):
    course_id = service.db.add_course("Independent study", "custom", None, source_course_ids=[])
    service.memory.add("I study this course on Fridays", course_id)
    tools = MemoryTools(
        service, ChatMessageInput(message="Schedule", course_id=course_id), "c", "m"
    )
    assert tools.context()["memories"][0]["course_id"] == course_id


def test_memory_api_works_without_model_and_exports_versioned_edits(service):
    assert not service.store.config()["model"]
    app = FastAPI()
    app.include_router(build_memory_router(service), prefix="/api/chat")
    with TestClient(app) as client:
        assert client.get("/api/chat/memories").json() == {"mode": "automatic", "memories": []}
        response = client.post(
            "/api/chat/memories", json={"text": "中文回答", "course_id": "test:math"}
        )
        assert response.status_code == 200
        memory = response.json()
        update = client.put(
            f"/api/chat/memories/{memory['id']}",
            json={
                "text": "中英双语回答",
                "expected_version": memory["version"],
            },
        )
        assert update.status_code == 200 and update.json()["course_id"] == "test:math"
        assert (
            client.delete(
                f"/api/chat/memories/{memory['id']}", params={"version": memory["version"]}
            ).status_code
            == 409
        )
        exported = client.get("/api/chat/memories/export")
        assert "attachment" in exported.headers["content-disposition"]
        assert "中英双语回答" in exported.text and "test:math" in exported.text
        assert client.put("/api/chat/memories/settings", json={"mode": "confirm"}).json() == {
            "mode": "confirm"
        }
        current = update.json()
        assert (
            client.delete(
                f"/api/chat/memories/{memory['id']}", params={"version": current["version"]}
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/chat/memories/{memory['id']}",
                json={
                    "text": "Do not recreate",
                    "expected_version": current["version"],
                },
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/chat/memories", json={"text": "x", "course_id": "unknown"}
            ).status_code
            == 400
        )
        assert client.post("/api/chat/memories", json={"text": "x" * 1001}).status_code == 422
        assert client.post("/api/chat/memories", json={"text": "api_key=abc123"}).status_code == 400
