import json

from fastapi.testclient import TestClient

from coursedeck.app import create_app
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, now
from coursedeck.mail import MailMessage, MailRule, MailRulePreview, MailStore


def fixture(tmp_path):
    db = Database(tmp_path / "mail.sqlite3")
    courses = [
        Course(provider="brightspace", external_id=str(i), name=name)
        for i, name in enumerate(["Calculus II", "Writing Workshop"])
    ]
    db.apply("brightspace", SyncResult(outcome=Outcome.SUCCESS, courses=courses), now().isoformat())
    return db, MailStore(db), courses


def mail(key, **changes):
    return MailMessage.model_validate(
        {
            "id": key,
            "sender": "Instructor",
            "sender_email": "instructor@example.test",
            "subject": "Weekly update",
            "body": "Read chapter one.",
            "body_complete": True,
        }
        | changes
    )


def dump(db):
    with db.connection() as conn:
        return list(conn.iterdump())


def test_preview_is_read_only_even_before_defaults_initialization(tmp_path):
    db, store, _ = fixture(tmp_path)
    store.upsert(mail("one", body="Please reply to this survey."))
    before = dump(db)
    result = store.preview_rule(
        MailRulePreview(
            rule=MailRule(field="sender", contains="instructor@example.test", action="ignore")
        )
    )
    assert result["counts"]["matched"] == 1 and result["counts"]["newly_ignored"] == 1
    assert result["messages"][0]["before"]["categories"] == ["Reply requested", "Survey"]
    assert dump(db) == before


def test_preview_covers_hidden_mail_and_preserves_manual_conflicts_and_incomplete(tmp_path):
    db, store, courses = fixture(tmp_path)
    for key in ["normal", "restored", "deleted", "incomplete", "conflict"]:
        store.upsert(mail(key))
    store.patch("restored", {"ignored": False})
    store.patch("deleted", {"deleted": True})
    store.upsert(mail("incomplete", snippet="changed", body="", body_complete=False))
    store.upsert(mail("conflict", body="Calculus II and Writing Workshop."))
    before = dump(db)
    proposal = MailRulePreview(
        rule=MailRule(field="sender", contains="instructor@example.test", action="ignore"), limit=2
    )
    result = store.preview_rule(proposal)
    assert result["counts"]["cached"] == 5 and result["counts"]["matched"] == 5
    assert result["counts"]["newly_ignored"] == 2
    assert result["counts"]["manual_overrides"] == 1 and result["counts"]["incomplete"] == 1
    assert result["has_more"] and len(result["messages"]) == 2
    rest = store.preview_rule(proposal.model_copy(update={"offset": 2, "limit": 20}))
    messages = {item["id"]: item for item in result["messages"] + rest["messages"]}
    assert messages["deleted"]["deleted"] and messages["deleted"]["after"]["ignored"]
    assert not messages["restored"]["after"]["ignored"]
    assert not messages["incomplete"]["after"]["ignored"]
    assert messages["conflict"]["after"]["candidate_course_ids"] == sorted(c.id for c in courses)
    assert not messages["conflict"]["after"]["ignored"]
    assert dump(db) == before


def test_preview_edit_disable_delete_and_reset_match_applied_results(tmp_path):
    _, store, courses = fixture(tmp_path)
    store.upsert(mail("one", body="Calculus II"))
    key = store.add_rule(
        MailRule(
            field="sender", contains="instructor@example.test", action="course", value=courses[0].id
        )
    )
    proposed = MailRule(
        field="sender", contains="instructor@example.test", action="course", value=courses[1].id
    )
    preview = store.preview_rule(MailRulePreview(operation="update", id=key, rule=proposed))
    assert preview["counts"]["new_conflicts"] == 1
    store.update_rule(key, proposed)
    assert store.get("one")["classification"] == "unclassifiable"
    disabled = proposed.model_copy(update={"enabled": False})
    preview = store.preview_rule(MailRulePreview(operation="update", id=key, rule=disabled))
    assert preview["messages"][0]["after"]["classification"] == "classified"
    preview = store.preview_rule(MailRulePreview(operation="delete", id=key))
    assert preview["counts"]["classification_changed"] == 1
    store.delete_rule(key)
    assert store.get("one")["course_id"] == courses[0].id
    for rule in store.rules():
        store.delete_rule(rule["id"])
    store.upsert(mail("one", body="Deadline extended. Please reply."))
    preview = store.preview_rule(MailRulePreview(operation="reset-defaults"))
    assert preview["counts"]["attention_changed"] == 1
    assert not store.get("one")["attention"]
    store.reset_defaults()
    assert store.get("one")["attention"] and store.get("one")["categories"] == [
        "Deadline change",
        "Reply requested",
    ]


def test_priority_is_explicit_optional_stable_and_does_not_change_task_state(tmp_path):
    db, store, _ = fixture(tmp_path)
    for key, body, timestamp in [
        ("old", "Please reply.", "2026-09-01T12:00:00Z"),
        ("new", "Read chapter one.", "2026-09-10T12:00:00Z"),
        ("middle", "Due date changed.", "2026-09-09T12:00:00Z"),
        ("negated", "No action required.", "2026-09-08T12:00:00Z"),
    ]:
        store.upsert(mail(key, body=body, received_at=timestamp))
    assert [item["id"] for item in store.list()["messages"]] == ["new", "middle", "negated", "old"]
    assert [item["id"] for item in store.list(order="attention")["messages"]] == [
        "middle",
        "old",
        "new",
        "negated",
    ]
    assert [item["id"] for item in store.list(attention_only=True, limit=1)["messages"]] == [
        "middle"
    ]
    assert store.list(attention_only=True, limit=1)["total"] == 2
    assert not store.get("negated")["attention"] and store.get("negated")["categories"] == []
    assert db.tasks() == []
    key = store.add_rule(
        MailRule(
            field="body", contains="chapter one", action="category", value="Read", priority=True
        )
    )
    assert store.get("new")["attention_reasons"] == ["Read"]
    store.delete_rule(key)
    assert not store.get("new")["attention"]


def test_keyword_migration_does_not_restore_deleted_old_defaults_or_reapply_new_defaults(tmp_path):
    db, store, _ = fixture(tmp_path)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO connector_states VALUES ('mail_rules', ?)",
            (json.dumps({"defaults_initialized": True}),),
        )
        conn.execute(
            "INSERT INTO mail_rules VALUES (?, ?)",
            (
                "builtin:survey",
                MailRule(
                    field="subject",
                    contains="form",
                    action="category",
                    value="Forms",
                    enabled=False,
                ).model_dump_json(),
            ),
        )
    before = dump(db)
    preview = store.preview_rule(MailRulePreview(operation="reset-defaults"))
    assert preview["counts"]["cached"] == 0 and dump(db) == before
    rules = {rule["id"]: rule for rule in store.rules()}
    assert "builtin:action-required" not in rules
    assert rules["builtin:survey"]["contains"] == "form" and not rules["builtin:survey"]["enabled"]
    assert "builtin:please-reply" in rules
    for rule in store.rules():
        store.delete_rule(rule["id"])
    assert MailStore(Database(db.path)).rules() == []


def test_preview_api_validation_and_priority_list_params(tmp_path):
    app = create_app(tmp_path)
    store = MailStore(app.state.db)
    store.upsert(mail("one", body="Please reply."))
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-CourseDeck": "1"}
    proposal = {"rule": {"field": "subject", "contains": "weekly", "action": "ignore"}}
    assert client.post("/api/mail/rules/preview", json=proposal).status_code == 403
    assert (
        client.post("/api/mail/rules/preview", json=proposal, headers=headers).json()["counts"][
            "newly_ignored"
        ]
        == 1
    )
    assert (
        client.post(
            "/api/mail/rules/preview",
            json={"operation": "delete", "id": "missing"},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/mail/rules/preview", json={"operation": "update"}, headers=headers
        ).status_code
        == 404
    )
    assert client.post("/api/mail/rules/preview", json={}, headers=headers).status_code == 400
    assert client.get("/api/mail?attention_only=true&order=attention").json()["total"] == 1
    assert client.get("/api/mail?order=invalid").status_code == 422
