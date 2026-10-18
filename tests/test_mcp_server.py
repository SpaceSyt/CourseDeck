import hashlib
import sys
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.mcp_server import CollectionClient, create_server, workspace_identity


def seed(directory):
    db = Database(directory / "coursedeck.sqlite3")
    db.apply(
        "gradescope",
        SyncResult(
            outcome=Outcome.SUCCESS,
            courses=[Course(provider="gradescope", external_id="c", name="Algorithms")],
            tasks=[
                Task(
                    provider="gradescope",
                    course_external_id="c",
                    external_id=str(i),
                    title=f"Homework {i}",
                    description="Long instructions. " * 1000,
                    score="8",
                    points_possible="10",
                    submission_status="submitted",
                )
                for i in range(3)
            ],
        ),
        now().isoformat(),
    )
    KnowledgeStore(directory / "knowledge.sqlite3").close()


def hashes(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.is_file()
    }


async def exercise(client):
    listing = await client.list_tools()
    assert len(listing.tools) == 10
    for tool in listing.tools:
        assert tool.input_schema and tool.output_schema
        assert tool.annotations.read_only_hint
    result = await client.call_tool("search_tasks", {"limit": 1})
    assert not result.is_error
    value = result.structured_content
    assert len(value["items"]) == 1
    assert value["pagination"]["has_more"]
    key = value["items"][0]["id"]
    detail = await client.call_tool("get_task", {"task_id": key, "limit": 100})
    assert not detail.is_error
    assert detail.structured_content["pagination"]["has_more"]
    assert len(detail.structured_content["content"]) == 100
    for name, args in [
        ("search_tasks", {"limit": 100000}),
        ("read_document", {"document_id": "../../credentials"}),
        ("run_sql", {"sql": "DELETE FROM tasks"}),
        ("request_collection", {"request": {"source": "gradescope", "scope": "source"}}),
    ]:
        error = await client.call_tool(name, args)
        assert error.is_error


async def test_sdk_tools_and_cache_side_effects(tmp_path):
    seed(tmp_path)
    before = hashes(tmp_path)
    async with Client(create_server(tmp_path)) as client:
        await exercise(client)
    assert hashes(tmp_path) == before


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_stdio_discovery_and_calls(tmp_path, mode):
    seed(tmp_path)
    before = hashes(tmp_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coursedeck.mcp_server", "--data-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
    )
    async with Client(params, read_timeout_seconds=30, mode=mode) as client:
        await exercise(client)
    assert hashes(tmp_path) == before


async def test_collection_discovery_is_explicit(tmp_path):
    async with Client(create_server(tmp_path, allow_collection=True)) as client:
        listing = await client.list_tools()
        tools = {t.name: t for t in listing.tools}
        assert len(tools) == 12
        assert not tools["request_collection"].annotations.read_only_hint
        assert tools["request_collection"].output_schema
        result = await client.call_tool("get_collection_job", {"job_id": "../../settings"})
        assert result.is_error


@pytest.mark.parametrize(
    "capability",
    [
        {"workspace_id": "wrong", "enabled": True},
        {"enabled": False},
    ],
)
async def test_collection_refuses_wrong_app_or_disabled(tmp_path, monkeypatch, capability):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=capability)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError):
        await CollectionClient(tmp_path, 48321).request("POST", "/api/collection/jobs", {})
    assert len(calls) == 1 and calls[0].method == "GET"


async def test_collection_uses_only_loopback_explicit_headers(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("capabilities"):
            return httpx.Response(
                200,
                json={
                    "workspace_id": workspace_identity(tmp_path),
                    "enabled": True,
                },
            )
        assert request.headers["X-CourseDeck-Collection"] == "1"
        assert request.headers["X-CourseDeck"] == "1"
        return httpx.Response(
            429,
            json={
                "detail": {
                    "category": "budget_exhausted",
                    "message": "Hourly budget reached",
                }
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="budget_exhausted"):
        await CollectionClient(tmp_path, 48321).request("POST", "/api/collection/jobs", {})
    assert all(r.url.host == "127.0.0.1" for r in calls)
