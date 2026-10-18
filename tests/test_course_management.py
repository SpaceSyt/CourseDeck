import pytest
from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now


def sync(db, name="Original course"):
    course = Course(provider="gradescope", external_id="1", name=name)
    task = Task(provider="gradescope", course_external_id="1", external_id="2", title="Homework")
    db.apply(
        "gradescope",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    return course, task


def test_alias_clearing_disabled_and_deleted_survive_sync_and_restore(tmp_path):
    db = Database(tmp_path / "db")
    course, task = sync(db)
    db.patch_local(task.id, {"note": "Keep note", "dismissed": True})
    db.patch_course(course.id, {"alias": "  Math  ", "disabled": True})
    assert db.courses()[0]["name"] == "Math"
    group = db.update_course_sources(course.id, "Original course", [course.id], alias="Math")
    db.patch_course(group, {"deleted": True})
    sync(db, "Updated source name")
    saved = db.courses()[0]
    assert saved["disabled"] and saved["deleted"] and saved["name"] == "Math"
    assert saved["original_name"] == "Updated source name"
    db.patch_course(group, {"alias": None, "deleted": False, "disabled": False})
    assert db.courses()[0]["name"] == "Updated source name"
    sync(db, "Latest source name")
    assert db.courses()[0]["name"] == "Latest source name"
    assert not db.courses()[0]["alias"]
    assert db.tasks()[0]["local"]["note"] == "Keep note"
    assert db.tasks()[0]["local"]["dismissed"]
    assert Database(db.path).courses() == db.courses()


def test_v5_existing_names_migrate_without_inventing_an_alias_later(tmp_path):
    db = Database(tmp_path / "db")
    course, _ = sync(db)
    key = db.add_course("Original course", "gradescope", course.id)
    with db.connection() as conn:
        conn.execute("DROP TABLE course_preferences")
        conn.execute("PRAGMA user_version=5")
        conn.execute("DROP TABLE schema_migrations")
    db = Database(db.path)
    sync(db, "Renamed by source")
    assert db.courses()[0]["name"] == "Renamed by source"
    assert db.courses()[0]["alias"] is None
    db.patch_course(key, {"alias": "Local name"})
    assert db.courses()[0]["name"] == "Local name"


def test_course_management_api_is_local_and_retains_data(tmp_path):
    app = create_app(tmp_path)
    course, _ = sync(app.state.db)
    client = TestClient(app, base_url="http://127.0.0.1")
    path = f"/api/courses/{course.id}"
    headers = {"X-CourseDeck": "1"}
    assert client.delete(path).status_code == 403
    assert client.patch(path, json={"alias": "My course"}, headers=headers).status_code == 200
    assert client.patch(path, json={"disabled": True}, headers=headers).status_code == 200
    assert client.delete(path, headers=headers).status_code == 200
    assert len(app.state.db.tasks()) == 1
    saved = app.state.db.courses()[0]
    assert saved["deleted"] and saved["disabled"] and saved["alias"] == "My course"
    assert (
        client.patch(path, json={"alias": "", "deleted": False}, headers=headers).status_code == 200
    )
    assert app.state.db.courses()[0]["name"] == "Original course"
    assert app.state.db.courses()[0]["disabled"]
    assert (
        client.patch("/api/courses/missing", json={"disabled": True}, headers=headers).status_code
        == 404
    )


def test_course_color_survives_alias_clear_binding_and_sync(tmp_path):
    db = Database(tmp_path / "db")
    course, _ = sync(db)
    db.patch_course(course.id, {"color": "#12ABef", "alias": "Math"})
    group = db.update_course_sources(course.id, course.name, [course.id], alias=None)
    sync(db, "Source rename")
    saved = db.courses()[0]
    assert saved["color"] == "#12abef" and saved["name"] == "Source rename"
    source = db.source_courses()[0]
    assert source["workspace_id"] == group and source["color"] == "#12abef"
    other = db.add_course("Other", "gradescope", None, color="#12abef")
    assert {course["color"] for course in db.courses()} == {"#12abef"}
    db.patch_course(other, {"color": None})
    assert next(course for course in db.courses() if course["id"] == other)["color"] is None
    assert Database(db.path).courses() == db.courses()


@pytest.mark.parametrize("color", ["red", "#abc", "#12345678", "#GGGGGG", "", 123456])
def test_invalid_course_color_rolls_back_the_whole_edit(tmp_path, color):
    db = Database(tmp_path / "db")
    course, _ = sync(db)
    before = db.courses()
    with pytest.raises(ValueError):
        db.update_course_sources(course.id, "Changed", [course.id], alias="Changed", color=color)
    assert db.courses() == before


def test_course_content_tasks_preserve_identity_and_notes(tmp_path):
    db = Database(tmp_path / "db")
    course = Course(provider="brightspace", external_id="1", name="Course")
    material = Task(
        provider="brightspace",
        course_external_id="1",
        external_id="content-2",
        title="Lecture slides",
    )
    assignment = material.model_copy(update={"external_id": "folder-3", "title": "Homework"})
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[material, assignment]),
        now().isoformat(),
    )
    db.patch_local(material.id, {"note": "Keep reading note"})
    all_tasks = {task["id"]: task for task in db.tasks()}
    assert set(all_tasks) == {assignment.id, material.id}
    assert all_tasks[material.id]["local"]["note"] == "Keep reading note"
    assert not all_tasks[material.id]["archived"]
    assert len(Database(db.path).tasks()) == 2
