import hashlib

import httpx
import pytest

from coursedeck.app import create_app
from coursedeck.cache_api import QUERY_TOOLS
from coursedeck.mcp_server import workspace_identity


async def test_rest_queries_share_schemas_and_have_no_cache_writes(tmp_path):
    app = create_app(tmp_path)
    try:
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob("*.sqlite3*")
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            spec = (await client.get("/openapi.json")).json()
            for method, path in QUERY_TOOLS.items():
                route = "/api/cache/" + path
                assert spec["paths"][route]["get"]["responses"]["200"]["content"][
                    "application/json"
                ]["schema"]
                if method in {"get_task", "read_document", "read_mail"}:
                    continue
                response = await client.get(route)
                assert response.status_code == 200, response.text
                assert response.json()["coverage"] == "cached_only"
            invalid = await client.get("/api/cache/tasks?limit=100000")
            assert invalid.status_code == 422
            capability = (await client.get("/api/collection/capabilities")).json()
            assert capability == {
                "workspace_id": workspace_identity(tmp_path),
                "enabled": False,
            }
            denied = await client.post(
                "/api/collection/jobs",
                headers={"X-CourseDeck": "1", "X-CourseDeck-Collection": "1"},
                json={"source": "gradescope", "scope": "source"},
            )
            assert denied.status_code == 403
        after = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob("*.sqlite3*")
        }
        assert before == after
    finally:
        await app.state.engine.close()
        await app.state.gmail.close()
        app.state.close_readers()


@pytest.mark.parametrize("environment,enabled", [("1", True), ("0", False), ("true", False)])
async def test_collection_app_enable_is_explicit(tmp_path, monkeypatch, environment, enabled):
    monkeypatch.setenv("COURSEDECK_ALLOW_COLLECTION", environment)
    app = create_app(tmp_path)
    try:
        assert app.state.collection_jobs.enabled is enabled
    finally:
        await app.state.engine.close()
        await app.state.gmail.close()
        app.state.close_readers()
