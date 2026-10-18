import hashlib
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from coursedeck.app import create_app
from coursedeck.cache_query import ReadOnlyDatabase
from coursedeck.mcp_server import create_server


@pytest.fixture
async def cache_app(tmp_path):
    app = create_app(tmp_path)
    try:
        yield app
    finally:
        await app.state.engine.close()
        await app.state.gmail.close()
        app.state.close_readers()


def cache_hashes(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "route,params",
    [
        ("task", {"task_id": "missing"}),
        ("document", {"document_id": "missing"}),
        ("mail-message", {"mail_id": "missing"}),
        ("tasks", {"course_id": "missing"}),
    ],
)
async def test_missing_cached_records_are_not_login_errors(cache_app, tmp_path, route, params):
    before = cache_hashes(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cache_app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/api/cache/" + route, params=params)
    assert response.status_code == 404
    assert response.json()["detail"]["category"] == "not_found"
    assert "not found in cache" in response.json()["detail"]["message"]
    assert cache_hashes(tmp_path) == before


@pytest.mark.parametrize(
    "route,params",
    [
        ("tasks", {"due_before": "2026-10-05"}),
        ("tasks", {"due_after": "2026-10-06T00:00:00Z", "due_before": "2026-10-05T00:00:00Z"}),
        ("tasks", {"due_before": "private-filter-value"}),
        ("mail", {"before": "2026-02-30"}),
    ],
)
async def test_invalid_cache_filters_have_safe_query_errors(cache_app, tmp_path, route, params):
    before = cache_hashes(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cache_app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/api/cache/" + route, params=params)
    assert response.status_code == 400
    assert response.json()["detail"]["category"] == "invalid_query"
    assert "cached query" in response.json()["detail"]["message"]
    assert "private-filter-value" not in response.text
    assert cache_hashes(tmp_path) == before


@pytest.mark.parametrize("condition", ["missing", "journal", "restore", "corrupt", "schema"])
async def test_unavailable_cache_is_not_an_empty_result(cache_app, tmp_path, condition):
    cache_app.state.close_readers()
    path = tmp_path / "coursedeck.sqlite3"
    if condition == "missing":
        path.unlink()
    elif condition == "journal":
        path.with_name(path.name + "-journal").write_bytes(b"pending transaction")
    elif condition == "restore":
        pending = tmp_path / "backups" / "restore-pending.json"
        pending.parent.mkdir(exist_ok=True)
        pending.write_text("{}")
    elif condition == "corrupt":
        path.write_bytes(b"not a database; private-cache-value")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE tasks")
    before = cache_hashes(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cache_app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/api/cache/tasks")
    assert response.status_code == 503
    if condition == "restore":
        # The application's maintenance gate deliberately intercepts all API reads.
        assert "Database recovery" in response.json()["detail"]
        assert response.headers["retry-after"] == "3"
    else:
        assert response.json()["detail"]["category"] == "cache_unavailable"
    assert "private-cache-value" not in response.text
    assert str(tmp_path) not in response.text
    if condition == "journal":
        assert "recovery or transaction is pending" in response.json()["detail"]["message"]
    assert cache_hashes(tmp_path) == before


@pytest.mark.parametrize("mode", ["in_process", "auto", "legacy"])
async def test_cache_errors_reach_mcp_clients(cache_app, tmp_path, mode):
    before = cache_hashes(tmp_path)
    server = (
        create_server(tmp_path)
        if mode == "in_process"
        else StdioServerParameters(
            command=sys.executable,
            args=["-m", "coursedeck.mcp_server", "--data-dir", str(tmp_path)],
            cwd=Path(__file__).resolve().parents[1],
        )
    )
    async with Client(
        server, mode="auto" if mode == "in_process" else mode, read_timeout_seconds=30
    ) as client:
        for tool, args, message in [
            ("get_task", {"task_id": "missing"}, "not_found: Task not found in cache"),
            ("read_document", {"document_id": "missing"}, "not_found: Document not found"),
            ("read_mail", {"mail_id": "missing"}, "not_found: Mail not found"),
            ("search_tasks", {"due_before": "2026-10-05"}, "timestamps with timezone"),
            ("search_tasks", {"due_before": "private-filter-value"}, "valid ISO 8601 timestamps"),
        ]:
            result = await client.call_tool(tool, args)
            assert result.is_error
            assert message in str(result.content)
            assert "private-filter-value" not in str(result.content)
        journal = tmp_path / "coursedeck.sqlite3-journal"
        journal.write_bytes(b"pending transaction")
        blocked = cache_hashes(tmp_path)
        result = await client.call_tool("search_tasks", {})
        assert result.is_error
        assert "recovery or transaction is pending" in str(result.content)
        assert cache_hashes(tmp_path) == blocked
        journal.unlink()
    assert cache_hashes(tmp_path) == before


@pytest.mark.parametrize(
    "exception,status,category",
    [
        (PermissionError("private-exception-value"), 503, "cache_unavailable"),
        (sqlite3.DatabaseError("private-exception-value"), 503, "cache_unavailable"),
        (ValueError("private-exception-value"), 400, "invalid_query"),
        (RuntimeError("private-exception-value"), 500, None),
    ],
)
async def test_untrusted_exception_text_stays_private(
    cache_app, tmp_path, monkeypatch, exception, status, category
):
    def fail(self):
        raise exception

    monkeypatch.setattr(ReadOnlyDatabase, "tasks", fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cache_app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/api/cache/tasks")
    assert response.status_code == status
    assert "private-exception-value" not in response.text
    if category:
        assert response.json()["detail"]["category"] == category
    async with Client(create_server(tmp_path)) as client:
        result = await client.call_tool("search_tasks", {})
    assert result.is_error
    assert "private-exception-value" not in str(result.content)
    if category:
        assert category in str(result.content)
