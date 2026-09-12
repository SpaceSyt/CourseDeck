import json

import pytest
from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.mail import CustomTaskInput, MailMessage, MailRule, MailStore, save_custom_task


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
        conn.execute("DROP TABLE schema_migrations")
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


def test_merge_keeps_target_settings_task_identity_and_all_local_references(tmp_path):
    db = Database(tmp_path / "db")
    courses, tasks = seed(db)
    target = db.add_course("Math", "gradescope", courses[0].id, color="#123456")
    source = db.add_course("Calculus homework", "webassign", courses[2].id, color="#654321")
    db.patch_course(source, {"disabled": True})
    db.patch_local(tasks[2].id, {"note": "Original note", "dismissed": True})
    custom = save_custom_task(db, CustomTaskInput(title="Review", course_id=source))
    db.patch_local(custom, {"note": "Custom note"})
    mail = MailStore(db)
    mail.upsert(MailMessage(id="linked", sender="Instructor", subject="Notes", body_complete=True))
    mail.patch("linked", {"course_id": source, "starred": True})
    rule = mail.add_rule(
        MailRule(field="sender", contains="Instructor", action="course", value=source)
    )
    with pytest.raises(ValueError):
        db.update_course_sources(target, "Math", [courses[0].id, courses[2].id])
    assert db.merge_courses(target, [source, source]) == target
    grouped = next(course for course in db.courses() if course.get("workspace_id") == target)
    assert grouped["name"] == "Math" and grouped["color"] == "#123456"
    assert not grouped["disabled"] and grouped["source_course_ids"] == [
        courses[0].id,
        courses[2].id,
    ]
    assert "Calculus homework" in grouped["source_names"]
    rows = {task["id"]: task for task in db.tasks()}
    assert rows[tasks[2].id]["source_course_id"] == courses[2].id
    assert rows[tasks[2].id]["local"]["note"] == "Original note"
    assert rows[tasks[2].id]["local"]["dismissed"]
    assert rows[custom]["course_id"] == grouped["id"]
    assert rows[custom]["local"]["note"] == "Custom note"
    assert mail.get("linked")["course_id"] == grouped["id"] and mail.get("linked")["starred"]
    assert next(item for item in mail.rules() if item["id"] == rule)["value"] == target
    with db.connection() as connection:
        preferences = json.loads(
            connection.execute(
                "SELECT payload FROM course_preferences WHERE course_id=?", (target,)
            ).fetchone()[0]
        )
        assert preferences["merged_courses"][0]["preferences"]["color"] == "#654321"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    seed(db)
    assert len(db.courses()) == 2
    db.patch_course(target, {"alias": None})
    assert (
        next(course for course in db.courses() if course.get("workspace_id") == target)["name"]
        == courses[0].name
    )
    assert Database(db.path).tasks() == db.tasks()


def test_merge_raw_courses_migrates_references_and_missing_ids_are_atomic(tmp_path):
    db = Database(tmp_path / "db")
    courses, _ = seed(db)
    custom = save_custom_task(db, CustomTaskInput(title="Study", course_id=courses[2].id))
    db.patch_course(courses[0].id, {"color": "#123456", "alias": "Math"})
    before = db.courses()
    with pytest.raises(KeyError):
        db.merge_courses(courses[0].id, [courses[2].id, "missing"])
    assert db.courses() == before
    key = db.merge_courses(courses[0].id, [courses[2].id])
    assert (
        next(course for course in db.courses() if course.get("workspace_id") == key)["color"]
        == "#123456"
    )
    with db.connection() as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM custom_tasks WHERE id=?", (custom,)).fetchone()[
                0
            ]
        )
        assert payload["course_id"] == key


def test_merge_into_unbound_course_preserves_its_name_and_adopts_all_links(tmp_path):
    db = Database(tmp_path / "db")
    courses, _ = seed(db)
    target = db.add_course("My math", "gradescope", None, color="#abcdef")
    source = db.add_course("Homework", "webassign", courses[2].id)
    assert db.merge_courses(target, [source]) == target
    grouped = next(course for course in db.courses() if course.get("workspace_id") == target)
    assert grouped["name"] == "My math" and grouped["color"] == "#abcdef"
    assert grouped["source_course_ids"] == [courses[2].id]
    with pytest.raises(ValueError):
        db.merge_courses(target, [target])


def test_merge_api_is_explicit_local_and_retains_destination_color(tmp_path):
    app = create_app(tmp_path)
    courses, _ = seed(app.state.db)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-CourseDeck": "1"}
    assert (
        client.patch(
            f"/api/courses/{courses[0].id}", json={"color": "#AABBCC"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/courses/{courses[0].id}", json={"color": "red"}, headers=headers
        ).status_code
        == 422
    )
    path = f"/api/courses/{courses[0].id}/merge"
    body = {"course_ids": [courses[2].id]}
    assert client.post(path, json=body).status_code == 403
    assert (
        client.post(path, json={"source_course_ids": [courses[2].id]}, headers=headers).status_code
        == 422
    )
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    grouped = next(
        course
        for course in app.state.db.courses()
        if course.get("workspace_id") == response.json()["id"]
    )
    assert grouped["color"] == "#aabbcc"
    assert set(grouped["source_course_ids"]) == {courses[0].id, courses[2].id}
    assert all(
        source["color"] == "#aabbcc"
        for source in app.state.db.source_courses()
        if source["id"] in grouped["source_course_ids"]
    )


def test_course_alias_resolution_survives_chained_workspace_merges(tmp_path):
    db = Database(tmp_path / "db")
    courses, _ = seed(db)
    first = db.add_course("First", "gradescope", courses[0].id)
    second = db.add_course("Second", "webassign", courses[2].id)
    final = db.add_course("Final", "gradescope", courses[1].id)
    assert db.resolve_course_alias(second) == courses[2].id
    db.merge_courses(first, [second])
    assert db.resolve_course_alias(second) == courses[0].id
    db.merge_courses(final, [first])
    for old_id in [first, second, final, *(course.id for course in courses)]:
        assert db.resolve_course_alias(old_id) == courses[1].id
    assert db.resolve_course_alias("missing") is None
    assert db.resolve_course_alias(None) is None
    assert Database(db.path).resolve_course_alias(second) == courses[1].id
    # Detaching a raw source makes that source current again, but old local scopes stay merged.
    db.update_course_sources(final, "Final", [courses[1].id, courses[0].id])
    assert db.resolve_course_alias(courses[2].id) == courses[2].id
    assert db.resolve_course_alias(second) == courses[1].id
