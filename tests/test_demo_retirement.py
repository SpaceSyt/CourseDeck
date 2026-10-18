from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now


def test_upgrade_removes_demo_only_and_cannot_reenable_it(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    for provider in ("demo", "google_classroom"):
        course = Course(provider=provider, external_id="same", name=provider)
        task = Task(
            provider=provider, course_external_id="same", external_id="same", title=provider
        )
        db.apply(
            provider,
            SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
            now().isoformat(),
        )
        db.patch_local(task.id, {"note": provider, "dismissed": True})
        db.add_course(provider, provider, course.id)
        db.update_state(provider, authorized=True)
    real = next(task for task in db.tasks() if task["provider"] == "google_classroom")
    with db.connection() as con:
        con.execute("PRAGMA user_version=2")
    app = create_app(tmp_path)
    upgraded = app.state.db
    assert upgraded.tasks() == [real]
    assert len(upgraded.courses()) == 1
    assert upgraded.courses()[0]["provider"] == "google_classroom"
    assert upgraded.state("demo") == {}
    assert upgraded.state("google_classroom")["authorized"]
    assert all(row["provider"] != "demo" for row in upgraded.history())
    with upgraded.connection() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 7
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("SELECT COUNT(*) FROM task_local_states").fetchone()[0] == 1
    # Retrying startup does not alter the surviving records.
    assert Database(upgraded.path).tasks() == [real]
    client = TestClient(app, base_url="http://127.0.0.1")
    assert {s["key"] for s in client.get("/api/snapshot").json()["sources"]} == {
        "google_classroom",
        "gradescope",
        "webassign",
        "brightspace",
        "rephactor",
    }
    assert (
        client.post("/api/sources/demo/connect", headers={"X-CourseDeck": "1"}).status_code == 404
    )
