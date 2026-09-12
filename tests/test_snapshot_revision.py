import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient
from test_core import snapshot

from coursedeck.app import create_app
from coursedeck.domain import Settings, now


def client_for(tmp_path):
    app = create_app(tmp_path)
    app.state.db.save_settings(Settings(startup_sync=False))
    app.state.db.apply("test", snapshot(), now().isoformat())
    return app, TestClient(app, base_url="http://127.0.0.1", headers={"X-CourseDeck": "1"})


def test_unchanged_snapshot_skips_tasks_and_all_writes_invalidate(tmp_path, monkeypatch):
    app, client = client_for(tmp_path)
    initial = client.get("/api/snapshot")
    assert initial.status_code == 200 and initial.headers["etag"]
    headers = {"If-None-Match": initial.headers["etag"]}
    original = app.state.db.tasks
    monkeypatch.setattr(app.state.db, "tasks", lambda: (_ for _ in ()).throw(AssertionError()))
    assert client.get("/api/snapshot", headers=headers).status_code == 304
    monkeypatch.setattr(app.state.db, "tasks", original)
    app.state.db.patch_local("test:c:a", {"note": "Edited"})
    changed = client.get("/api/snapshot", headers=headers)
    assert changed.status_code == 200 and changed.json()["tasks"][0]["local"]["note"] == "Edited"
    assert changed.headers["etag"] != initial.headers["etag"]
    headers = {"If-None-Match": changed.headers["etag"]}
    # External writers and restore do not pass through application invalidation hooks.
    app.state.db.backup(tmp_path / "before.sqlite3")
    with closing(sqlite3.connect(app.state.db.path)) as connection, connection:
        connection.execute("UPDATE settings SET payload=?", ('{"startup_sync":true}',))
    assert client.get("/api/snapshot", headers=headers).status_code == 200
    current = client.get("/api/snapshot").headers["etag"]
    with closing(sqlite3.connect(tmp_path / "before.sqlite3")) as source:
        with closing(sqlite3.connect(app.state.db.path)) as target:
            source.backup(target)
    restored = client.get("/api/snapshot", headers={"If-None-Match": current})
    assert restored.status_code == 200 and not restored.json()["settings"]["startup_sync"]


def test_live_engine_state_and_process_restart_invalidate(tmp_path):
    app, client = client_for(tmp_path)
    initial = client.get("/api/snapshot").headers["etag"]
    app.state.engine.revision += 1
    assert client.get("/api/snapshot", headers={"If-None-Match": initial}).status_code == 200
    newest = client.get("/api/snapshot").headers["etag"]
    other = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    assert other.get("/api/snapshot", headers={"If-None-Match": newest}).status_code == 200


def test_ui_history_and_undo_preserve_source_and_reject_stale_version(tmp_path):
    app, client = client_for(tmp_path)
    path = "/api/tasks/test:c:a/local"
    receipt = "/api/task-edits/test:c:a"
    source = snapshot().tasks[0].submission_status
    initial = client.get(receipt).json()
    assert initial["changes"] == []
    assert (
        client.patch(
            path, json={"note": "Local note", "expected_version": initial["version"]}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            path, json={"pinned": True, "expected_version": initial["version"]}
        ).status_code
        == 409
    )
    history = client.get(receipt).json()
    change = history["changes"][0]
    assert change["fields"] == ["note"]
    assert (
        client.post(
            receipt + "/undo",
            json={"change_id": change["id"], "expected_version": initial["version"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            receipt + "/undo",
            json={"change_id": change["id"], "expected_version": history["version"]},
        ).status_code
        == 200
    )
    current = app.state.db.tasks()[0]
    assert current["local"]["note"] == "" and current["submission_status"] == source
    assert client.get(receipt).json()["changes"][0]["undone"]
