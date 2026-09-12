"""Chat edits deterministic mail rules, bound to the current user's explicit instruction."""

import re

from .mail import MailRule, MailRulePreview, MailStore

QUOTED = r'["“]([^"”]+)["”]'
FIELDS = {
    "发件人": "sender",
    "标题": "subject",
    "正文": "body",
    "任意位置": "any",
    "所有位置": "any",
    "sender": "sender",
    "subject": "subject",
    "body": "body",
    "anywhere": "any",
}


def requested_edit(message, store):
    text = re.sub(r"^(?:请(?:帮我)?|帮我|please\s+)", "", message.strip(), flags=re.I)
    text = text.rstrip("。.!！").strip()
    rules = store.rules(initialize=False)

    def target(value):
        matches = [
            rule
            for rule in rules
            if value.casefold() in {rule["id"].casefold(), rule["contains"].casefold()}
        ]
        return matches[0] if len(matches) == 1 else None

    def course(value):
        matches = [
            c
            for c in store.db.courses()
            if not c.get("disabled")
            and not c.get("deleted")
            and value.casefold()
            in {
                str(c.get(k) or "").casefold()
                for k in ("id", "workspace_id", "name", "source_name")
            }
        ]
        return matches[0]["id"] if len(matches) == 1 else None

    match = re.fullmatch(r"(启用|禁用|删除)(?:邮件)?规则\s*" + QUOTED, text)
    if not match:
        match = re.fullmatch(r"(enable|disable|delete)\s+(?:mail\s+)?rule\s+" + QUOTED, text, re.I)
    if match:
        old = target(match[2])
        if not old:
            return None
        if match[1].lower() in {"删除", "delete"}:
            return MailRulePreview(operation="delete", id=old["id"])
        return MailRulePreview(
            operation="update",
            id=old["id"],
            rule=MailRule.model_validate(
                {k: v for k, v in old.items() if k != "id"}
                | {"enabled": match[1].lower() in {"启用", "enable"}}
            ),
        )

    match = re.fullmatch(
        r"(?:把|将)?(?:邮件)?规则\s*"
        + QUOTED
        + r"(?:的)?(关键词|匹配位置|关联课程|分类)(?:改为|改成|设为)\s*"
        + QUOTED,
        text,
    )
    if not match:
        match = re.fullmatch(
            r"(?:change|set)\s+(?:mail\s+)?rule\s+"
            + QUOTED
            + r"\s+(keyword|field|course|category)\s+to\s+"
            + QUOTED,
            text,
            re.I,
        )
    if match:
        old = target(match[1])
        if not old:
            return None
        key, value = match[2].lower(), match[3]
        patch = {}
        if key in {"关键词", "keyword"}:
            patch = {"contains": value}
        elif key in {"匹配位置", "field"} and value.lower() in FIELDS:
            patch = {"field": FIELDS[value.lower()]}
        elif key in {"关联课程", "course"} and course(value):
            patch = {"action": "course", "value": course(value)}
        elif key in {"分类", "category"}:
            patch = {"action": "category", "value": value}
        if patch:
            return MailRulePreview(
                operation="update",
                id=old["id"],
                rule=MailRule.model_validate({k: v for k, v in old.items() if k != "id"} | patch),
            )
        return None

    match = re.fullmatch(
        r"(?:新增|添加)(?:一条)?(?:邮件)?规则[：:]?\s*"
        r"(发件人|标题|正文|任意位置|所有位置)(?:包含|含有)\s*"
        + QUOTED
        + r"[，,；;\s]*(?:则)?(关联(?:到)?课程|归到课程|分类为|自动忽略|无分类)\s*(?:"
        + QUOTED
        + r")?",
        text,
    )
    if not match:
        match = re.fullmatch(
            r"add\s+(?:a\s+)?mail rule:\s*"
            r"(sender|subject|body|anywhere) contains\s+"
            + QUOTED
            + r"\s*->\s*(course|category|ignore|none)\s*(?:"
            + QUOTED
            + r")?",
            text,
            re.I,
        )
    if not match:
        # Common course-association request; unspecified location means anywhere.
        short = re.fullmatch(
            r"把(?:包含|含有)\s*" + QUOTED + r"\s*的邮件(?:关联到|归到)\s*" + QUOTED, text
        )
        if short and course(short[2]):
            return MailRulePreview(
                operation="add",
                rule=MailRule(
                    field="any", contains=short[1], action="course", value=course(short[2])
                ),
            )
        return None
    field, keyword, action, value = match.groups()
    action = action.lower()
    if action in {"关联课程", "关联到课程", "归到课程", "course"}:
        action, value = "course", course(value) if value else None
    elif action in {"分类为", "category"}:
        action = "category"
    else:
        if value:
            return None
        action = "ignore" if action in {"自动忽略", "ignore"} else "none"
        value = ""
    if value is None or action == "category" and not value:
        return None
    return MailRulePreview(
        operation="add",
        rule=MailRule(field=FIELDS[field.lower()], contains=keyword, action=action, value=value),
    )


class MailRuleTools:
    def __init__(self, service, message):
        self.store = MailStore(service.db)
        self.message = message
        self.enabled = bool(re.search(r"mail|邮件|邮箱|规则|关键词", message, re.I))
        try:
            self.intent = requested_edit(message, self.store) if self.enabled else None
        except ValueError:
            self.intent = None
        self.version = None
        self.receipt = None

    def definitions(self, define):
        if not self.enabled:
            return []
        result = [
            define(
                "list_mail_rules",
                "Read local mail rules and course names, not emails. "
                "Use the explicit command examples when clarifying a rule edit.",
                {
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                [],
            )
        ]
        if self.intent:
            result += [
                define(
                    "preview_mail_rule_edit",
                    "Preview the exact mail rule edit authorized "
                    "by the current user message. Returns impact counts and a version. "
                    "Does not modify rules or expose email content.",
                    {},
                    [],
                ),
                define(
                    "apply_mail_rule_edit",
                    "Apply only that authorized edit after preview. "
                    "Never change Gmail itself. Re-preview on version conflict.",
                    {"expected_version": {"type": "string", "minLength": 64, "maxLength": 64}},
                    ["expected_version"],
                ),
            ]
        return result

    def guard(self):
        current = requested_edit(self.message, self.store)
        if not self.intent or current != self.intent:
            raise ValueError("Rule or course matching changed; clarify the requested edit")

    async def run(self, name, args):
        if not self.enabled:
            raise ValueError("Mail rule tools are unavailable for this request")
        if name == "list_mail_rules":
            offset, limit = args.get("offset", 0), args.get("limit", 50)
            if (
                set(args) - {"offset", "limit"}
                or type(offset) is not int
                or offset < 0
                or type(limit) is not int
                or not 1 <= limit <= 50
            ):
                raise ValueError("Invalid pagination")
            rules = self.store.rules(initialize=False)
            return {
                "rules": rules[offset : offset + limit],
                "total": len(rules),
                "has_more": offset + limit < len(rules),
                "courses": [
                    {"id": c["id"], "name": c["name"]}
                    for c in self.store.db.courses()
                    if not c.get("disabled") and not c.get("deleted")
                ],
                "edit_available": self.intent is not None,
                "examples": [
                    "把包含“Intro to Programming”的邮件关联到“Programming”",
                    "把邮件规则“survey”的关键词改成“course survey”",
                    "禁用邮件规则“survey”",
                    "删除邮件规则“survey”",
                    "新增邮件规则：标题包含“newsletter”，自动忽略",
                    'Add mail rule: subject contains "survey" -> category "Survey"',
                ],
                "automatic_name_matching": "Course names and aliases are also matched; "
                "these are not editable mail rules.",
            }
        if not self.intent:
            raise ValueError("An explicit, unambiguous mail rule edit is required")
        if name == "preview_mail_rule_edit":
            if args:
                raise ValueError("The requested edit is already bound to the user instruction")
            self.guard()
            before = self.store.rule_edit_version()
            impact = self.store.preview_rule(self.intent)
            if before != self.store.rule_edit_version():
                raise ValueError("Preview inputs changed; preview again")
            self.version = before
            return {
                "edit": self.intent.model_dump(mode="json"),
                "counts": impact["counts"],
                "version": before,
                "preview_only": True,
            }
        if name == "apply_mail_rule_edit":
            if (
                set(args) != {"expected_version"}
                or not self.version
                or args["expected_version"] != self.version
            ):
                raise ValueError("Preview this edit first and use its current version")
            if self.receipt is None:
                self.receipt = self.store.apply_rule_edit(
                    self.intent, self.version, guard=self.guard
                )
                names = {
                    key: course["name"]
                    for course in self.store.db.courses()
                    for key in (course["id"], course.get("workspace_id"))
                    if key
                }
                for side in ("before", "after"):
                    rule = self.receipt.get(side)
                    if rule and rule["action"] == "course":
                        self.receipt[side] = rule | {
                            "course_name": names.get(rule["value"], "Unavailable course")
                        }
            return self.receipt
        raise ValueError("Unknown mail rule tool")
