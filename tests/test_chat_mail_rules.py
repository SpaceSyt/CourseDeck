import json

import pytest
from test_chat import configured, reply, seed
from test_chat_tools import call

from coursedeck.chat import ChatMessageInput, tool_definition
from coursedeck.chat_mail_rules import MailRuleTools, requested_edit
from coursedeck.mail import MailMessage, MailRule, MailStore


@pytest.mark.parametrize(
    "message",
    [
        "把包含“Intro to Programming”的邮件关联到“Math”",
        "新增邮件规则：标题包含“Intro to Programming”，关联到课程“Math”",
        'Add mail rule: subject contains "Intro to Programming" -> course "Math"',
    ],
)
def test_explicit_course_rules(message, tmp_path):
    store = MailStore(seed(tmp_path))
    # Actual fixture course is Algebra; match its exact current name.
    course = store.db.courses()[0]
    message = message.replace("Math", course["name"])
    edit = requested_edit(message, store)
    assert edit.operation == "add" and edit.rule.contains == "Intro to Programming"
    assert edit.rule.value == course["id"]


@pytest.mark.parametrize(
    "message",
    [
        "不要删除邮件规则“survey”",
        "为什么要禁用邮件规则“survey”？",
        "如果删除邮件规则“survey”会怎样",
        '"删除邮件规则“survey”"',
        "邮件中写着：删除邮件规则“survey”",
        "禁用邮件规则“survey”？",
    ],
)
def test_negated_quoted_and_hypothetical_messages_do_not_grant_edits(message, tmp_path):
    assert requested_edit(message, MailStore(seed(tmp_path))) is None


async def test_read_preview_apply_are_bounded_idempotent_and_do_not_export_mail(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    store = MailStore(service.db)
    store.upsert(
        MailMessage(
            id="mail",
            sender="PRIVATE SENDER",
            subject="newsletter",
            body="PRIVATE BODY",
            body_complete=True,
        )
    )
    tools = MailRuleTools(service, "新增邮件规则：标题包含“newsletter”，自动忽略")
    listed = await tools.run("list_mail_rules", {})
    assert "PRIVATE BODY" not in json.dumps(listed) and "PRIVATE SENDER" not in json.dumps(listed)
    with pytest.raises(ValueError, match="Preview"):
        await tools.run("apply_mail_rule_edit", {"expected_version": "a" * 64})
    before = store.rules(initialize=False)
    preview = await tools.run("preview_mail_rule_edit", {})
    assert store.rules(initialize=False) == before
    assert preview["counts"]["newly_ignored"] == 1
    assert "PRIVATE" not in json.dumps(preview)
    with pytest.raises(ValueError):
        await tools.run(
            "apply_mail_rule_edit", {"expected_version": preview["version"], "rule": {}}
        )
    result = await tools.run("apply_mail_rule_edit", {"expected_version": preview["version"]})
    assert result["changed"] and result["after"]["action"] == "ignore"
    assert store.get("mail")["ignored"]
    assert (
        await tools.run("apply_mail_rule_edit", {"expected_version": preview["version"]}) == result
    )
    again = MailRuleTools(service, tools.message)
    preview = await again.run("preview_mail_rule_edit", {})
    assert not (await again.run("apply_mail_rule_edit", {"expected_version": preview["version"]}))[
        "changed"
    ]
    assert len([r for r in store.rules() if r["contains"] == "newsletter"]) == 1


async def test_stale_preview_and_target_ambiguity_cannot_write(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    tools = MailRuleTools(service, "禁用邮件规则“survey”")
    preview = await tools.run("preview_mail_rule_edit", {})
    tools.store.add_rule(MailRule(field="body", contains="survey", action="none"))
    with pytest.raises(ValueError, match="changed"):
        await tools.run("apply_mail_rule_edit", {"expected_version": preview["version"]})
    assert tools.store.rules()[0]["enabled"]
    assert requested_edit(tools.message, tools.store) is None
    with pytest.raises(ValueError, match="matching changed"):
        await tools.run("preview_mail_rule_edit", {})


async def test_rule_preview_does_not_treat_stale_body_as_current_evidence(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    store = MailStore(service.db)
    store.upsert(
        MailMessage(
            id="stale-mail",
            sender="PRIVATE SENDER",
            subject="Old thread",
            body="PRIVATE NEWSLETTER",
            body_complete=True,
            content_key="text-links-v1:old",
        )
    )
    store.upsert(
        MailMessage(
            id="stale-mail",
            sender="PRIVATE SENDER",
            subject="New reply",
            content_key="text-links-v1:new",
        )
    )
    tools = MailRuleTools(service, 'Add mail rule: body contains "NEWSLETTER" -> ignore')
    preview = await tools.run("preview_mail_rule_edit", {})
    assert preview["counts"]["incomplete"] == 1
    assert preview["counts"]["newly_ignored"] == 0
    assert "PRIVATE" not in json.dumps(preview)
    assert store.get("stale-mail")["body"] == "PRIVATE NEWSLETTER"


async def test_keyword_update_enable_disable_delete_and_manual_choices(tmp_path):
    service = await configured(tmp_path, lambda _: reply())
    store = MailStore(service.db)
    store.upsert(
        MailMessage(id="mail", sender="sender", subject="course survey", body_complete=True)
    )
    store.patch("mail", {"course_id": "none", "ignored": False})
    for message in [
        "把邮件规则“survey”的关键词改成“course survey”",
        "禁用邮件规则“course survey”",
        "启用邮件规则“course survey”",
        "删除邮件规则“course survey”",
    ]:
        tools = MailRuleTools(service, message)
        assert tools.intent
        preview = await tools.run("preview_mail_rule_edit", {})
        result = await tools.run("apply_mail_rule_edit", {"expected_version": preview["version"]})
        assert result["changed"]
        assert not store.get("mail")["ignored"] and store.get("mail")["course_override"] == "none"
    assert all(rule["contains"] != "course survey" for rule in store.rules())


async def test_chat_runner_persists_receipt_and_only_exposes_tools_for_mail_requests(tmp_path):
    def handler(request):
        payload = json.loads(request.content)
        outputs = [
            json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"
        ]
        if not outputs:
            return reply(None, [call("preview_mail_rule_edit")])
        if len(outputs) == 1:
            return reply(
                None, [call("apply_mail_rule_edit", expected_version=outputs[0]["version"])]
            )
        assert outputs[1]["changed"]
        return reply("Mail rule saved.")

    service = await configured(tmp_path, handler)
    assert not MailRuleTools(service, "Explain algebra").definitions(tool_definition)
    response = await service.send(ChatMessageInput(message="禁用邮件规则“survey”"))
    events = service.store.conversation(response["conversation_id"])["messages"][-1]["activity"]
    receipts = [item["mail_rule_change"] for item in events if "mail_rule_change" in item]
    assert receipts and receipts[0]["after"]["enabled"] is False
