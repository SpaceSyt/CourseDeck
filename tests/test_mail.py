from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, now
from coursedeck.mail import (
    DEFAULT_MAIL_RULES,
    CustomTaskInput,
    MailMessage,
    MailRule,
    MailStore,
    save_custom_task,
)


def setup(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    courses = [
        Course(provider="brightspace", external_id=str(i), name=name)
        for i, name in enumerate(["Calculus II", "Intro to Writing"])
    ]
    db.apply("brightspace", SyncResult(outcome=Outcome.PARTIAL, courses=courses), now().isoformat())
    return db, MailStore(db), courses


def message(key="one", **changes):
    return MailMessage(
        id=key,
        sender="Instructor",
        sender_email="teacher@example.test",
        **(
            {"subject": "Calculus II", "body": "Please complete the survey.", "body_complete": True}
            | changes
        ),
    )


def test_classification_distinguishes_none_conflict_and_missing_body(tmp_path):
    _, store, courses = setup(tmp_path)
    for key, subject, body, complete, expected in [
        ("match", "Calculus II", "Homework", True, "classified"),
        ("none", "Campus event", "Welcome", True, "none"),
        ("conflict", "Calculus II", "Intro to Writing", True, "unclassifiable"),
        ("incomplete", "Calculus II", "", False, "unclassifiable"),
    ]:
        store.upsert(message(key, subject=subject, body=body, body_complete=complete))
        assert store.get(key)["classification"] == expected
    assert store.get("match")["course_id"] == courses[0].id
    store.patch("conflict", {"course_id": "none"})
    assert store.get("conflict")["classification"] == "none"
    store.patch("conflict", {"course_id": courses[1].id})
    assert store.get("conflict")["course_id"] == courses[1].id
    store.patch("conflict", {"course_id": "auto"})
    assert store.get("conflict")["classification"] == "unclassifiable"


def test_rules_ignore_restore_and_sync_preserve_local_state(tmp_path):
    _, store, courses = setup(tmp_path)
    store.upsert(message())
    assert store.get("one")["categories"] == ["Survey"]
    assert not store.get("one")["ignored"]
    key = store.add_rule(MailRule(field="body", contains="survey", action="ignore"))
    assert store.list()["total"] == 0
    assert store.list(ignored=True)["total"] == 1
    store.patch("one", {"ignored": False, "deleted": True, "starred": True})
    store.upsert(message(body="Please complete the survey. Updated."))
    assert store.list()["total"] == 0
    mail = store.list(deleted=True)["messages"][0]
    assert mail["starred"] and not mail["ignored"]
    assert "body" not in mail
    store.patch("one", {"deleted": False})
    assert store.list()["total"] == 1
    # A conflicting classification is never automatically ignored.
    store.upsert(message("conflict", body="Intro to Writing survey"))
    assert not store.get("conflict")["ignored"]
    store.delete_rule(key)
    assert key not in {rule["id"] for rule in store.rules()}
    store.add_rule(
        MailRule(
            field="sender", contains="teacher@example.test", action="course", value=courses[1].id
        )
    )
    assert store.get("one")["classification"] == "unclassifiable"


def test_default_keywords_can_be_disabled_edited_deleted_and_explicitly_restored(tmp_path):
    db, store, _ = setup(tmp_path)
    store.upsert(message())
    defaults = {rule["id"]: rule for rule in store.rules()}
    assert set(defaults) == set(DEFAULT_MAIL_RULES)
    survey = MailRule.model_validate(
        {k: v for k, v in defaults["builtin:survey"].items() if k != "id"}
    )
    store.update_rule("builtin:survey", survey.model_copy(update={"enabled": False}))
    assert store.get("one")["categories"] == []
    restarted = MailStore(Database(db.path))
    assert not next(r for r in restarted.rules() if r["id"] == "builtin:survey")["enabled"]
    assert restarted.get("one")["categories"] == []
    restarted.update_rule("builtin:survey", survey.model_copy(update={"value": "Questionnaire"}))
    assert restarted.get("one")["categories"] == ["Questionnaire"]
    for rule in restarted.rules():
        restarted.delete_rule(rule["id"])
    restarted = MailStore(Database(db.path))
    assert restarted.rules() == []
    assert restarted.get("one")["categories"] == []
    custom = restarted.add_rule(
        MailRule(field="body", contains="complete", action="category", value="Reply")
    )
    restarted.reset_defaults()
    assert custom in {r["id"] for r in restarted.rules()}
    assert restarted.get("one")["categories"] == ["Reply", "Survey"]


def test_custom_keyword_edits_reclassify_cached_mail_without_overriding_local_choices(tmp_path):
    _, store, courses = setup(tmp_path)
    store.upsert(message(subject="Homework", body="Please review worksheet Alpha."))
    key = store.add_rule(
        MailRule(field="body", contains="Alpha", action="course", value=courses[0].id)
    )
    assert store.get("one")["course_id"] == courses[0].id
    store.update_rule(
        key, MailRule(field="body", contains="Alpha", action="course", value=courses[1].id)
    )
    assert store.get("one")["course_id"] == courses[1].id
    store.add_rule(MailRule(field="body", contains="Alpha", action="course", value=courses[0].id))
    store.add_rule(MailRule(field="body", contains="Alpha", action="ignore"))
    assert store.get("one")["classification"] == "unclassifiable"
    assert not store.get("one")["ignored"]
    store.patch("one", {"course_id": "none", "ignored": False, "deleted": True})
    store.update_rule(
        key, MailRule(field="body", contains="Beta", action="category", value="Later")
    )
    mail = store.get("one")
    assert mail["classification"] == "none" and not mail["ignored"] and mail["deleted"]
    assert "Later" not in mail["categories"]


def test_existing_custom_rules_are_preserved_when_defaults_are_first_seeded(tmp_path):
    db, store, _ = setup(tmp_path)
    legacy = {"field": "subject", "contains": "worksheet", "action": "category", "value": "Read"}
    with db.connection() as conn:
        import json

        conn.execute("INSERT INTO mail_rules VALUES (?, ?)", ("legacy", json.dumps(legacy)))
    rules = {r["id"]: r for r in store.rules()}
    assert rules["legacy"] == legacy | {"id": "legacy", "enabled": True, "priority": False}
    assert len(rules) == len(DEFAULT_MAIL_RULES) + 1
    store.upsert(message(subject="worksheet", body="", body_complete=True))
    assert store.get("one")["categories"] == ["Read"]


def test_changed_thread_never_reuses_stale_body_for_classification(tmp_path):
    _, store, _ = setup(tmp_path)
    store.upsert(message(snippet="old"))
    store.upsert(message(snippet="old", body="", body_complete=False))
    assert store.get("one")["body_complete"]
    store.upsert(message(snippet="new", body="", body_complete=False))
    assert not store.get("one")["body_complete"]
    assert store.get("one")["classification"] == "unclassifiable"
    store.upsert(message("thread", content_key="old"))
    store.upsert(message("thread", content_key="new", body="", body_complete=False))
    assert not store.get("thread")["body_complete"]


def test_custom_tasks_survive_lms_sync_binding_and_mail_deletion(tmp_path):
    db, store, courses = setup(tmp_path)
    local = db.add_course("My Writing", "brightspace", None)
    store.upsert(message())
    independent = save_custom_task(db, CustomTaskInput(title="Buy notebooks"))
    linked = save_custom_task(db, CustomTaskInput(title="Reply", course_id=local, email_id="one"))
    db.patch_local(linked, {"dismissed": True, "note": "Keep"})
    db.bind_course(local, courses[1].id)
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.SUCCESS, courses=courses, complete=True),
        now().isoformat(),
    )
    store.patch("one", {"deleted": True})
    tasks = {t["id"]: t for t in Database(db.path).tasks()}
    assert tasks[independent]["course_id"] is None
    assert tasks[linked]["course_id"] == courses[1].id
    assert tasks[linked]["local"]["note"] == "Keep" and tasks[linked]["local"]["dismissed"]
    save_custom_task(db, CustomTaskInput(title="Updated", email_id="one"), linked)
    assert next(t for t in db.tasks() if t["id"] == linked)["local"]["note"] == "Keep"
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_mail_endpoints_validation_isolation_and_pagination(tmp_path):
    app = create_app(tmp_path)
    store = MailStore(app.state.db)
    for i in range(55):
        store.upsert(message(str(i)))
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-CourseDeck": "1"}
    assert client.get("/api/mail").json()["has_more"]
    assert len(client.get("/api/mail?offset=50").json()["messages"]) == 5
    assert client.get("/api/mail?offset=-1").status_code == 422
    assert client.get("/api/mail?limit=9999").status_code == 422
    assert "body" not in str(client.get("/api/snapshot").json())
    assert client.post("/api/custom-tasks", json={"title": "New"}).status_code == 403
    for value in [{"title": " "}, {"title": "T", "due_at": "2026-09-08T12:00:00"}]:
        assert client.post("/api/custom-tasks", json=value, headers=headers).status_code == 422
    for value in [{"title": "T", "course_id": "bad"}, {"title": "T", "email_id": "bad"}]:
        assert client.post("/api/custom-tasks", json=value, headers=headers).status_code == 400
    response = client.post(
        "/api/custom-tasks", json={"title": "New", "email_id": "0"}, headers=headers
    )
    assert response.status_code == 201
    key = response.json()["id"]
    assert (
        client.patch(
            f"/api/tasks/{key}/local", json={"dismissed": True}, headers=headers
        ).status_code
        == 200
    )
    assert client.get("/api/mail/messages/0").json()["task_ids"] == [key]
    assert client.get("/api/mail/messages/missing").status_code == 404
    assert (
        client.patch("/api/mail/messages/0", json={"course_id": "bad"}, headers=headers).status_code
        == 400
    )


def test_mail_rule_edit_endpoints_validate_and_update_cached_messages(tmp_path):
    app = create_app(tmp_path)
    store = MailStore(app.state.db)
    store.upsert(message(subject="Campus", body="Fill in the questionnaire.", body_complete=True))
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-CourseDeck": "1"}
    rules = client.get("/api/mail/rules").json()
    original = next(rule for rule in rules if rule["id"] == "builtin:survey")
    payload = {key: value for key, value in original.items() if key != "id"}
    payload["contains"] = "questionnaire"
    path = "/api/mail/rules/builtin:survey"
    assert client.put(path, json=payload).status_code == 403
    assert client.put(path, json=payload | {"field": "bad"}, headers=headers).status_code == 422
    assert client.put(path, json=payload | {"value": ""}, headers=headers).status_code == 400
    assert client.put("/api/mail/rules/missing", json=payload, headers=headers).status_code == 404
    assert client.put(path, json=payload, headers=headers).json() == {"id": "builtin:survey"}
    assert client.get("/api/mail/messages/one").json()["categories"] == ["Survey"]
    assert client.delete(path, headers=headers).status_code == 200
    assert client.get("/api/mail/messages/one").json()["categories"] == []
    assert client.post("/api/mail/rules/reset-defaults").status_code == 403
    assert client.post("/api/mail/rules/reset-defaults", headers=headers).status_code == 200
    restored = client.get("/api/mail/rules").json()
    assert next(rule for rule in restored if rule["id"] == "builtin:survey") == original
