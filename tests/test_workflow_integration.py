"""Exercise recovery across HTTP requests and both stores, using temporary data only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, Settings, SyncResult, Task, now

HEADERS = {"X-CourseDeck": "1"}


def prepared(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    db.save_settings(Settings(startup_sync=False))
    app = create_app(tmp_path)
    app.state.db.apply(
        "test",
        SyncResult(
            outcome=Outcome.SUCCESS,
            courses=[Course(provider="test", external_id="math", name="Math")],
            tasks=[
                Task(
                    provider="test",
                    course_external_id="math",
                    external_id="hw",
                    title="Homework",
                    submission_status="new",
                )
            ],
        ),
        now().isoformat(),
    )
    app.state.db.patch_local("test:math:hw", {"note": "Original note"})
    app.state.knowledge.upsert_documents(
        [
            {
                "id": "notes",
                "provider": "test",
                "course_id": "test:math",
                "title": "Notes",
                "body": "Original document",
                "source_task_id": "test:math:hw",
                "complete": True,
            }
        ]
    )
    return app


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    )


@pytest.mark.asyncio
async def test_backup_waits_for_existing_http_write_and_keeps_heartbeat_available(tmp_path):
    app = prepared(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    @app.post("/api/test-long-write")
    async def long_write():
        entered.set()
        await release.wait()
        app.state.db.patch_local("test:math:hw", {"note": "Finished before backup"})
        return {"ok": True}

    async with client_for(app) as client:
        existing = asyncio.create_task(client.post("/api/test-long-write"))
        await asyncio.wait_for(entered.wait(), 3)
        backup = asyncio.create_task(client.post("/api/recovery/backups"))
        async with asyncio.timeout(3):
            while not app.state.maintenance_gate.busy:
                await asyncio.sleep(0)
        assert not backup.done()
        heartbeat = (await client.get("/api/heartbeat")).json()
        assert heartbeat["maintenance"] and heartbeat["status"] == "connected"
        assert (await client.get("/api/snapshot")).status_code == 503
        assert (await client.post("/api/sync")).status_code == 503
        assert (await client.post("/api/recovery/backups")).status_code == 409
        release.set()
        assert (await existing).status_code == 200
        result = await backup
        assert result.status_code == 200, result.text
        stored = Database(tmp_path / "backups" / result.json()["id"] / "coursedeck.sqlite3")
        assert stored.tasks()[0]["local"]["note"] == "Finished before backup"
        assert (await client.get("/api/snapshot")).status_code == 200


@pytest.mark.asyncio
async def test_restore_preserves_paired_history_and_association_routes(tmp_path):
    app = prepared(tmp_path)
    knowledge = app.state.knowledge
    conversation = knowledge.create_conversation("Question", "test:math", "test:math:hw")
    knowledge.add_message(conversation, "assistant", "Read notes", [{"id": "notes"}])
    async with client_for(app) as client:
        relations = (await client.get("/api/associations/task/test:math:hw")).json()
        assert any(
            row["target"]["id"] == "notes" and row["active"] for row in relations["relations"]
        )
        saved = (await client.post("/api/recovery/backups")).json()["id"]
        app.state.db.patch_local("test:math:hw", {"note": "Later note"})
        knowledge.upsert_documents(
            [
                {
                    "id": "notes",
                    "provider": "test",
                    "course_id": "test:math",
                    "title": "Notes",
                    "body": "Later document",
                }
            ]
        )
        knowledge.add_message(conversation, "user", "Later question")
        result = await client.post("/api/recovery/restore", json={"id": saved})
        assert result.status_code == 200, result.text
        assert (await client.get("/api/snapshot")).json()["tasks"][0]["local"][
            "note"
        ] == "Original note"
        assert (await client.get("/api/chat/library/notes")).json()["body"] == "Original document"
        history = (await client.get("/api/chat/conversations/" + conversation)).json()
        assert len(history["messages"]) == 1 and history["messages"][0]["citations"] == [
            {"id": "notes"}
        ]
        assert history["task_id"] == "test:math:hw"
        assert (await client.get("/api/chat/task-context/test:math:hw")).json()["documents"][0][
            "id"
        ] == "notes"
        previous = result.json()["pre_restore_backup"]
        assert (await client.post("/api/recovery/preflight", json={"id": previous})).json()["valid"]
        assert not app.state.maintenance_gate.busy and not app.state.engine.paused


@pytest.mark.asyncio
async def test_interrupted_restore_blocks_normal_api_until_explicit_recovery(tmp_path):
    app = prepared(tmp_path)
    backup = app.state.recovery.create_backup()
    app.state.recovery.journal.write_text('{"target":"pending"}', encoding="utf-8")
    app = create_app(tmp_path)
    async with client_for(app) as client:
        assert (await client.get("/api/recovery/status")).json()["recovery_required"]
        for method, route in [
            ("GET", "/api/snapshot"),
            ("GET", "/api/chat/library"),
            ("POST", "/api/sync"),
        ]:
            assert (await client.request(method, route)).status_code == 503
        assert (await client.get("/api/heartbeat")).status_code == 200
        assert (await client.get("/api/recovery/backups")).json()["interrupted_restore"]
        restored = await client.post("/api/recovery/restore", json={"id": backup["id"]})
        assert restored.status_code == 200, restored.text
        assert not (await client.get("/api/recovery/status")).json()["recovery_required"]
        assert (await client.get("/api/snapshot")).status_code == 200


@pytest.mark.asyncio
async def test_pending_login_refuses_backup_without_closing_browser(tmp_path):
    app = prepared(tmp_path)
    app.state.gmail.browser = SimpleNamespace(interactive=True)
    close = AsyncMock()
    app.state.gmail.close = close
    async with client_for(app) as client:
        response = await client.post("/api/recovery/backups")
        assert response.status_code == 409 and "sign-in" in response.json()["detail"]
        close.assert_not_called()
        assert app.state.gmail.browser.interactive
        assert not app.state.maintenance_gate.busy
        assert (await client.get("/api/heartbeat")).status_code == 200


@pytest.mark.asyncio
async def test_failed_browser_close_resumes_mail_poll_without_duplicate_source_poll(tmp_path):
    app = prepared(tmp_path)
    started = {"mail": 0, "source": 0}

    async def poll(kind):
        started[kind] += 1
        await asyncio.Event().wait()

    app.state.gmail.poll = lambda: poll("mail")
    app.state.engine.poll = lambda: poll("source")
    app.state.gmail.close = AsyncMock(side_effect=[RuntimeError("Browser close failed"), None])
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(3):
            while started != {"mail": 1, "source": 1}:
                await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1",
            headers=HEADERS,
        ) as client:
            assert (await client.post("/api/recovery/backups")).status_code == 500
            async with asyncio.timeout(3):
                while started["mail"] != 2:
                    await asyncio.sleep(0)
            assert started["source"] == 1
            assert not app.state.maintenance_gate.busy
            assert (await client.get("/api/snapshot")).status_code == 200


@pytest.mark.asyncio
async def test_second_failed_recovery_never_releases_a_preexisting_journal(tmp_path, monkeypatch):
    app = prepared(tmp_path)
    manager = app.state.recovery
    target = manager.create_backup()
    app.state.db.patch_local("test:math:hw", {"note": "Later note"})
    app.state.knowledge.upsert_documents(
        [
            {
                "id": "notes",
                "provider": "test",
                "course_id": "test:math",
                "title": "Notes",
                "body": "Later document",
            }
        ]
    )
    rollback = manager.create_backup()
    # Simulate an interrupted two-file restore: tasks are A, documents are B.
    app.state.db.patch_local("test:math:hw", {"note": "Original note"})
    journal = {"target": target["id"], "rollback": rollback["id"]}
    manager.journal.write_text(json.dumps(journal), encoding="utf-8")
    original_copy = manager._copy_pair

    def fail_target(backup_id):
        if backup_id == target["id"]:
            raise OSError("Target unavailable")
        original_copy(backup_id)

    monkeypatch.setattr(manager, "_copy_pair", fail_target)
    async with client_for(app) as client:
        assert (await client.post("/api/recovery/backups")).status_code == 400
        assert (
            await client.post("/api/recovery/restore", json={"id": target["id"]})
        ).status_code == 400
        assert manager.journal.exists() and json.loads(manager.journal.read_text()) == journal
        assert (await client.get("/api/snapshot")).status_code == 503
        assert (await client.get("/api/recovery/status")).json()["recovery_required"]
        assert app.state.db.tasks()[0]["local"]["note"] == "Later note"
        assert app.state.knowledge.get("notes")["body"] == "Later document"
