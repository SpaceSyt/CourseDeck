from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.mail import CustomTaskInput, MailMessage, MailStore, save_custom_task


def seed(db):
    courses, tasks = [], []
    for provider, external_id, name in [
        ("gradescope", "calculus", "Calculus II"),
        ("gradescope", "programming", "Programming"),
        ("webassign", "calculus", "MA-UY 1124"),
    ]:
        course = Course(provider=provider, external_id=external_id, name=name)
        task = Task(
            provider=provider,
            external_id="homework",
            course_external_id=external_id,
            title="Homework",
        )
        db.apply(
            provider,
            SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[task]),
            now().isoformat(),
        )
        courses.append(course)
        tasks.append(task)
    return courses, tasks


def test_many_courses_per_source_and_multi_source_group_preserve_identity(tmp_path):
    db = Database(tmp_path / "db")
    courses, tasks = seed(db)
    assert len(db.courses()) == 3
    local = save_custom_task(db, CustomTaskInput(title="Study", course_id=courses[2].id))
    db.patch_local(tasks[2].id, {"note": "Keep this", "dismissed": True})
    store = MailStore(db)
    store.upsert(
        MailMessage(
            id="mail",
            sender="Instructor",
            subject="Calculus II / MA-UY 1124",
            body="Study",
            body_complete=True,
        )
    )
    assert store.get("mail")["classification"] == "unclassifiable"
    key = db.update_course_sources(courses[0].id, "Calculus", [courses[0].id, courses[2].id])
    grouped = next(c for c in db.courses() if c.get("workspace_id") == key)
    assert set(grouped["providers"]) == {"gradescope", "webassign"}
    assert len(db.courses()) == 2
    rows = {t["id"]: t for t in db.tasks()}
    assert rows[tasks[0].id]["course_id"] == rows[tasks[2].id]["course_id"] == grouped["id"]
    assert rows[tasks[2].id]["source_course_id"] == courses[2].id
    assert rows[tasks[2].id]["local"]["note"] == "Keep this"
    assert rows[local]["course_id"] == grouped["id"]
    assert store.get("mail")["classification"] == "classified"
    # A sync refresh cannot split the user's group or lose per-source local edits.
    seed(db)
    assert len(db.courses()) == 2
    assert next(t for t in db.tasks() if t["id"] == tasks[2].id)["local"]["dismissed"]
    db.update_course_sources(key, "Calculus", [courses[0].id])
    assert len(db.courses()) == 3
    assert next(t for t in db.tasks() if t["id"] == tasks[2].id)["course_id"] == courses[2].id
    with db.connection() as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_v4_migration_copies_existing_binding_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "db")
    courses, _ = seed(db)
    key = db.add_course("My Calculus", "gradescope", courses[0].id)
    before = db.tasks()
    with db.connection() as conn:
        conn.execute("DROP TABLE course_links")
        conn.execute("PRAGMA user_version=4")
    db = Database(db.path)
    assert db.tasks() == before
    assert next(c for c in db.courses() if c.get("workspace_id") == key)["source_course_ids"] == [
        courses[0].id
    ]
    assert Database(db.path).courses() == db.courses()


def test_multi_source_api_is_atomic_and_does_not_steal_links(tmp_path):
    app = create_app(tmp_path)
    courses, _ = seed(app.state.db)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-CourseDeck": "1"}
    data = {
        "name": "Calculus",
        "provider": "gradescope",
        "source_course_ids": [courses[0].id, courses[2].id],
    }
    response = client.post("/api/courses", json=data, headers=headers)
    assert response.status_code == 201
    key = response.json()["id"]
    before = app.state.db.courses()
    for ids in [[courses[0].id, "missing"], [courses[2].id]]:
        response = client.put(
            f"/api/courses/{courses[1].id}/sources",
            json={"name": "Other", "source_course_ids": ids},
            headers=headers,
        )
        assert response.status_code == 400
        assert app.state.db.courses() == before
    assert (
        client.put(
            f"/api/courses/{key}/sources",
            json={"name": "Math", "source_course_ids": [courses[0].id, courses[2].id]},
            headers=headers,
        ).status_code
        == 200
    )
