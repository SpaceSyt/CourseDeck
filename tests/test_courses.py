import sqlite3

from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now


def seed(db):
    course = Course(provider="google_classroom", external_id="123", name="Remote name")
    task = Task(
        provider=course.provider,
        course_external_id=course.external_id,
        external_id="456",
        title="Homework",
    )
    result = SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task])
    db.apply(course.provider, result, now().isoformat())
    db.patch_local(task.id, {"note": "Keep my work", "dismissed": True})
    return course, task, result


def test_migration_and_binding_preserve_source_identity_and_local_edits(tmp_path):
    db = Database(tmp_path / "db")
    course, task, result = seed(db)
    with sqlite3.connect(db.path) as con:
        con.execute("DROP TABLE workspace_courses")
        con.execute("PRAGMA user_version=1")
        con.execute("DROP TABLE schema_migrations")
    db = Database(db.path)
    local_id = db.add_course("My course name", course.provider, None)
    assert next(c for c in db.courses() if c["id"] == local_id)["needs_binding"]
    db.bind_course(local_id, course.id)
    result.courses[0].name = "Source renamed this"
    db.apply(course.provider, result, now().isoformat())
    assert len(db.courses()) == 1
    assert db.courses()[0]["name"] == "My course name"
    assert db.courses()[0]["workspace_id"] == local_id
    assert db.source_courses()[0]["name"] == "Source renamed this"
    saved = db.tasks()[0]
    assert saved["id"] == task.id and saved["course_id"] == course.id
    assert saved["local"]["note"] == "Keep my work" and saved["local"]["dismissed"]


def test_course_endpoints_validate_source_and_binding(tmp_path):
    app = create_app(tmp_path)
    course, _, _ = seed(app.state.db)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-CourseDeck": "1"}
    payload = {"name": "My name", "provider": "google_classroom", "remote_course_id": course.id}
    assert client.post("/api/courses", json=payload).status_code == 403
    assert client.post("/api/courses", json=payload, headers=headers).status_code == 201
    assert client.post("/api/courses", json=payload, headers=headers).status_code == 400
    for change, code in [
        ({"provider": "gradescope"}, 400),
        ({"provider": "unknown"}, 404),
        ({"name": "   "}, 422),
    ]:
        assert (
            client.post("/api/courses", json=payload | change, headers=headers).status_code == code
        )
    response = client.post(
        "/api/courses", json={"name": "Unlinked", "provider": "gradescope"}, headers=headers
    )
    key = response.json()["id"]
    assert (
        client.patch(
            f"/api/courses/{key}/binding", json={"remote_course_id": course.id}, headers=headers
        ).status_code
        == 400
    )
    assert (
        client.patch(
            "/api/courses/missing/binding", json={"remote_course_id": course.id}, headers=headers
        ).status_code
        == 404
    )
    snapshot = client.get("/api/snapshot").json()
    assert len(snapshot["courses"]) == 2 and len(snapshot["source_courses"]) == 1
    ping = client.get("/api/heartbeat")
    assert ping.json()["service"] == "coursedeck" and ping.json()["status"] == "connected"
    assert ping.headers["Cache-Control"] == "no-store"
