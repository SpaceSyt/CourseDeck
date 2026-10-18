import json

import pytest
from test_chat import configured, reply
from test_mail import message, setup

from coursedeck.chat_mail_rules import MailRuleTools, requested_edit
from coursedeck.mail import MailMessage, MailRule, MailRulePreview, MailStore


def test_filter_sender_is_exact_and_independent_of_incomplete_course_classification(tmp_path):
    _, store, _ = setup(tmp_path)
    for key, sender in [
        ("hit", "STAFF@example.test"),
        ("suffix", "otherstaff@example.test"),
        ("domain", "staff@example.test.evil"),
    ]:
        store.upsert(MailMessage(id=key, sender="Staff", sender_email=sender, subject="Notice"))
    rule = MailRule(field="sender", contains="staff@example.test", match="equals", action="filter")
    preview = store.preview_rule(MailRulePreview(rule=rule))
    assert preview["counts"]["newly_ignored"] == 1
    assert store.list()["total"] == 3
    key = store.add_rule(rule)
    assert store.get("hit")["classification"] == "unclassifiable"
    assert store.get("hit")["ignored"] and store.list()["total"] == 2
    assert store.list(ignored=True)["total"] == 3
    store.update_rule(key, rule.model_copy(update={"enabled": False}))
    assert store.list()["total"] == 3
    store.update_rule(key, rule)
    store.delete_rule(key)
    assert store.list()["total"] == 3


def test_keyword_filters_retain_cache_and_manual_restores_across_sync(tmp_path):
    _, store, _ = setup(tmp_path)
    mail = message(subject="Campus Newsletter", body_complete=False)
    store.upsert(mail)
    store.add_rule(MailRule(field="subject", contains="news", action="filter"))
    assert store.get("one")["ignored"] and store.list()["total"] == 0
    assert store.get("one")["body"] == mail.body
    store.patch("one", {"ignored": False})
    store.upsert(mail)
    assert store.list()["total"] == 1
    store.upsert(mail.model_copy(update={"id": "future"}))
    assert store.get("future")["ignored"] and store.list()["total"] == 1


def test_stale_body_cannot_trigger_a_filter_and_uncertainty_is_retained(tmp_path):
    _, store, _ = setup(tmp_path)
    store.upsert(
        message(subject="General note", body="Newsletter", body_complete=False, body_stale=True)
    )
    store.add_rule(MailRule(field="body", contains="newsletter", action="filter"))
    assert not store.get("one")["ignored"]
    assert store.get("one")["classification"] == "unclassifiable"
    store.upsert(message(subject="General note", body="Newsletter", body_complete=True))
    assert store.get("one")["ignored"]


@pytest.mark.parametrize(
    "command,field,match,keyword",
    [
        ("过滤发件人为“staff@example.test”的邮件", "sender", "equals", "staff@example.test"),
        ("屏蔽发信人是“staff@example.test”的邮件", "sender", "equals", "staff@example.test"),
        ("过滤包含“newsletter”的邮件", "any", "contains", "newsletter"),
        ("添加Inbox过滤规则：标题包含“Newsletter”", "subject", "contains", "Newsletter"),
        ('Filter emails from "staff@example.test"', "sender", "equals", "staff@example.test"),
    ],
)
def test_filter_commands(command, field, match, keyword, tmp_path):
    _, store, _ = setup(tmp_path)
    intent = requested_edit(command, store)
    assert intent.rule.action == "filter" and intent.rule.field == field
    assert intent.rule.match == match and intent.rule.contains == keyword


@pytest.mark.parametrize(
    "command",
    [
        "不要过滤包含“newsletter”的邮件",
        "过滤包含“newsletter”的邮件？",
        "邮件写着：过滤包含“newsletter”的邮件",
        "如果过滤包含“newsletter”的邮件会怎样",
    ],
)
def test_filter_instructions_are_not_inferred_from_questions_or_mail_text(command, tmp_path):
    _, store, _ = setup(tmp_path)
    assert requested_edit(command, store) is None


async def test_chat_filter_preview_apply_and_scoped_disable(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    store = MailStore(service.db)
    store.upsert(MailMessage(id="one", sender="Staff", sender_email="staff@example.test"))
    store.add_rule(
        MailRule(field="any", contains="staff@example.test", action="category", value="Staff")
    )
    tools = MailRuleTools(service, "过滤发件人为“staff@example.test”的邮件")
    preview = await tools.run("preview_mail_rule_edit", {})
    assert preview["counts"]["newly_ignored"] == 1
    assert not store.get("one")["ignored"]
    await tools.run("apply_mail_rule_edit", {"expected_version": preview["version"]})
    assert store.get("one")["ignored"]
    tools = MailRuleTools(service, "禁用过滤规则“staff@example.test”")
    preview = await tools.run("preview_mail_rule_edit", {})
    await tools.run("apply_mail_rule_edit", {"expected_version": preview["version"]})
    assert not store.get("one")["ignored"]
    assert all(r["enabled"] for r in store.rules() if r["action"] == "category")


def test_legacy_rules_gain_a_default_match_mode_without_database_rewrite(tmp_path):
    db, store, _ = setup(tmp_path)
    legacy = {"field": "subject", "contains": "newsletter", "action": "ignore", "value": ""}
    with db.connection() as conn:
        conn.execute("INSERT INTO mail_rules VALUES (?, ?)", ("legacy", json.dumps(legacy)))
    assert (
        next(r for r in store.rules(initialize=False) if r["id"] == "legacy")["match"] == "contains"
    )
    with db.connection() as conn:
        assert (
            json.loads(
                conn.execute("SELECT payload FROM mail_rules WHERE id=?", ("legacy",)).fetchone()[0]
            )
            == legacy
        )
