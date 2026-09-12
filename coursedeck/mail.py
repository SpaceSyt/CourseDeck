"""Local mail, deterministic rules and independently owned to-dos. No AI required."""

import hashlib
import json
import re
import unicodedata
from contextlib import nullcontext
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .domain import LocalState, now


class CustomTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=50000)
    due_at: AwareDatetime | None = None
    course_id: str | None = None
    email_id: str | None = None


class MailMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=512)
    sender: str = Field(max_length=1000)
    sender_email: str = Field(default="", max_length=500)
    subject: str = Field(default="", max_length=2000)
    snippet: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=500000)
    body_complete: bool = False
    received_at: AwareDatetime | None = None
    date_label: str = Field(default="", max_length=200)
    date_text: str = Field(default="", max_length=100)
    url: str = Field(default="", max_length=2000)
    unread: bool = False
    content_key: str = Field(default="", max_length=512)


class MailPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool | None = None
    ignored: bool | None = None
    starred: bool | None = None
    read: bool | None = None
    # 'auto' re-evaluates rules; 'none' explicitly means no course.
    course_id: str | None = None


class MailRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    field: Literal["sender", "subject", "body", "any"]
    contains: str = Field(min_length=2, max_length=200)
    action: Literal["course", "category", "ignore", "none"]
    value: str = Field(default="", max_length=500)
    enabled: bool = True
    priority: bool = False


DEFAULT_MAIL_RULES = {
    "builtin:action-required": MailRule(
        field="any",
        contains="action required",
        action="category",
        value="Action required",
        priority=True,
    ),
    "builtin:survey": MailRule(
        field="any", contains="survey", action="category", value="Survey", priority=True
    ),
    "builtin:please-reply": MailRule(
        field="any",
        contains="please reply",
        action="category",
        value="Reply requested",
        priority=True,
    ),
    "builtin:response-required": MailRule(
        field="any",
        contains="response required",
        action="category",
        value="Reply requested",
        priority=True,
    ),
    "builtin:deadline-extended": MailRule(
        field="any",
        contains="deadline extended",
        action="category",
        value="Deadline change",
        priority=True,
    ),
    "builtin:due-date-changed": MailRule(
        field="any",
        contains="due date changed",
        action="category",
        value="Deadline change",
        priority=True,
    ),
}
LEGACY_DEFAULT_IDS = {"builtin:action-required", "builtin:survey"}


class MailRulePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["add", "update", "delete", "reset-defaults"] = "add"
    id: str | None = Field(default=None, max_length=200)
    rule: MailRule | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def matches(needle, haystack):
    # Word boundaries prevent e.g. 'art' matching 'department'.
    return (
        re.search(r"(?<!\w)" + re.escape(normalized(needle)) + r"(?!\w)", normalized(haystack))
        is not None
    )


def mail_fields(mail):
    text = " ".join([mail["subject"], mail["snippet"], mail["body"]])
    return {
        "sender": mail["sender_email"] or mail["sender"],
        "subject": mail["subject"],
        "body": mail["body"],
        "any": text + " " + mail["sender_email"],
    }


def priority_match(rule, fields):
    text = normalized(fields[rule["field"]])
    pattern = re.compile(r"(?<!\w)" + re.escape(normalized(rule["contains"])) + r"(?!\w)")
    return any(
        not re.search(
            r"(?:\bno|\bnot|\bwithout|无需|不需要)\s*$",
            text[max(0, hit.start() - 20) : hit.start()],
        )
        for hit in pattern.finditer(text)
    )


def resolve_course(db, key):
    if key is None:
        return None
    for course in db.courses():
        if key in (course["id"], course.get("workspace_id"), *course.get("source_course_ids", [])):
            return course
    raise ValueError("Course not found")


def custom_tasks(db):
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM custom_tasks ORDER BY created_at").fetchall()
    result = []
    for row in rows:
        task = json.loads(row["payload"])
        course = resolve_course(db, task.get("course_id"))
        result.append(
            task
            | {
                "id": row["id"],
                "provider": "custom",
                "course_id": course["id"] if course else None,
                "email_id": row["email_id"],
                "url": None,
                "submission_status": "open",
                "available_at": None,
                "closes_at": None,
                "graded": False,
                "score": None,
                "points_possible": None,
                "archived": False,
                "missing_count": 0,
                "first_seen_at": row["created_at"],
                "last_seen_at": row["created_at"],
                "local": json.loads(row["local_payload"]),
            }
        )
    return result


def save_custom_task(db, value: CustomTaskInput, key=None):
    course = resolve_course(db, value.course_id)
    payload = value.model_dump(mode="json", exclude={"email_id"})
    payload["course_id"] = (course.get("workspace_id") or course["id"]) if course else None
    with db.connection() as conn:
        if (
            value.email_id
            and not conn.execute(
                "SELECT 1 FROM mail_messages WHERE id=?", (value.email_id,)
            ).fetchone()
        ):
            raise ValueError("Email not found")
        if key:
            if not conn.execute("SELECT 1 FROM custom_tasks WHERE id=?", (key,)).fetchone():
                raise KeyError(key)
            conn.execute(
                "UPDATE custom_tasks SET payload=?, email_id=? WHERE id=?",
                (json.dumps(payload), value.email_id, key),
            )
        else:
            key = "custom:" + uuid4().hex
            conn.execute(
                "INSERT INTO custom_tasks VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    json.dumps(payload),
                    value.email_id,
                    LocalState().model_dump_json(),
                    now().isoformat(),
                ),
            )
    return key


class MailStore:
    def __init__(self, db):
        self.db = db

    def upsert(self, message: MailMessage, sort_at=None):
        with self.db.connection() as conn:
            old = conn.execute(
                "SELECT payload FROM mail_messages WHERE id=?", (message.id,)
            ).fetchone()
            payload = message.model_dump(mode="json")
            if old and not message.body_complete:
                previous = json.loads(old[0])
                if (
                    previous.get("body_complete")
                    and previous["snippet"] == message.snippet
                    and previous["date_label"] == message.date_label
                    and previous.get("content_key", "") == message.content_key
                ):
                    payload.update(body=previous["body"], body_complete=True)
            stamp = (
                message.received_at.isoformat()
                if message.received_at
                else (sort_at or now().isoformat())
            )
            conn.execute(
                """INSERT INTO mail_messages (id, payload, received_at) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                received_at=CASE WHEN ? THEN excluded.received_at
                ELSE mail_messages.received_at END""",
                (
                    message.id,
                    json.dumps(payload),
                    stamp,
                    message.received_at is not None or sort_at is not None,
                ),
            )

    def rules(self, initialize=True):
        if initialize:
            self._initialize_rules()
        with self.db.connection() as conn:
            rules = [
                {
                    "enabled": True,
                    "priority": DEFAULT_MAIL_RULES[r["id"]].priority
                    if r["id"] in DEFAULT_MAIL_RULES
                    else False,
                }
                | json.loads(r["payload"])
                | {"id": r["id"]}
                for r in conn.execute("SELECT * FROM mail_rules ORDER BY rowid")
            ]
            row = conn.execute(
                "SELECT payload FROM connector_states WHERE provider='mail_rules'"
            ).fetchone()
            state = json.loads(row[0]) if row else {}
        existing = {rule["id"] for rule in rules}
        for key, rule in self._pending_defaults(state).items():
            if key not in existing:
                rules.append(rule.model_dump() | {"id": key})
        return rules

    @staticmethod
    def _pending_defaults(state):
        if state.get("defaults_version", 0) >= 2:
            return {}
        return {
            key: rule
            for key, rule in DEFAULT_MAIL_RULES.items()
            if not state.get("defaults_initialized") or key not in LEGACY_DEFAULT_IDS
        }

    def _initialize_rules(self, reset=False, connection=None):
        # Seed and marker must commit together; an empty rules table can be intentional.
        with nullcontext(connection) if connection is not None else self.db.connection() as conn:
            if connection is None:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM connector_states WHERE provider='mail_rules'"
            ).fetchone()
            state = json.loads(row[0]) if row else {}
            defaults = DEFAULT_MAIL_RULES if reset else self._pending_defaults(state)
            if not defaults and not reset:
                return
            for key, rule in defaults.items():
                conn.execute(
                    "INSERT INTO mail_rules VALUES (?, ?) ON CONFLICT(id) "
                    + ("DO UPDATE SET payload=excluded.payload" if reset else "DO NOTHING"),
                    (key, rule.model_dump_json()),
                )
            state["defaults_initialized"] = True
            state["defaults_version"] = 2
            conn.execute(
                "INSERT OR REPLACE INTO connector_states VALUES ('mail_rules', ?)",
                (json.dumps(state),),
            )

    def reset_defaults(self):
        self._initialize_rules(reset=True)

    def _validated_rule(self, rule):
        rule = rule.model_copy()
        if rule.action == "course":
            course = resolve_course(self.db, rule.value)
            rule.value = course.get("workspace_id") or course["id"]
        if rule.action == "category" and not rule.value:
            raise ValueError("Enter a category")
        if rule.action in {"none", "ignore"}:
            rule.value = ""
        return rule

    def add_rule(self, rule):
        rule = self._validated_rule(rule)
        self._initialize_rules()
        key = uuid4().hex
        with self.db.connection() as conn:
            conn.execute("INSERT INTO mail_rules VALUES (?, ?)", (key, rule.model_dump_json()))
        return key

    def update_rule(self, key, rule):
        rule = self._validated_rule(rule)
        self._initialize_rules()
        with self.db.connection() as conn:
            if not conn.execute(
                "UPDATE mail_rules SET payload=? WHERE id=?", (rule.model_dump_json(), key)
            ).rowcount:
                raise KeyError(key)
        return key

    def delete_rule(self, key):
        self._initialize_rules()
        with self.db.connection() as conn:
            if not conn.execute("DELETE FROM mail_rules WHERE id=?", (key,)).rowcount:
                raise KeyError(key)

    def rule_edit_version(self):
        # Include preview inputs: an arriving email or course regrouping invalidates old counts.
        with self.db.connection() as conn:
            messages = [
                tuple(row)
                for row in conn.execute(
                    "SELECT id,payload,local_payload FROM mail_messages ORDER BY id"
                )
            ]
        data = [self.rules(initialize=False), self.db.courses(), messages]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def apply_rule_edit(self, value: MailRulePreview, expected_version, *, guard=None):
        if value.operation not in {"add", "update", "delete"}:
            raise ValueError("Unsupported mail rule edit")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.rule_edit_version() != expected_version:
                raise ValueError("Mail rules or preview inputs changed; preview again")
            if guard:
                guard()
            rules = self.rules(initialize=False)
            before = next((rule for rule in rules if rule["id"] == value.id), None)
            if value.operation != "add" and before is None:
                raise ValueError("Mail rule no longer exists")
            after = self._validated_rule(value.rule).model_dump() if value.rule else None
            if value.operation != "delete" and after is None:
                raise ValueError("Enter a rule")
            key = value.id or uuid4().hex
            if value.operation == "add":
                duplicate = next(
                    (
                        rule
                        for rule in rules
                        if {k: v for k, v in rule.items() if k != "id"} == after
                    ),
                    None,
                )
                if duplicate:
                    return {
                        "changed": False,
                        "rule_id": duplicate["id"],
                        "before": duplicate,
                        "after": duplicate,
                    }
            self._initialize_rules(connection=conn)
            if value.operation == "delete":
                conn.execute("DELETE FROM mail_rules WHERE id=?", (key,))
            else:
                conn.execute(
                    "INSERT INTO mail_rules VALUES (?,?) ON CONFLICT(id) "
                    "DO UPDATE SET payload=excluded.payload",
                    (key, json.dumps(after)),
                )
                after = after | {"id": key}
            return {"changed": before != after, "rule_id": key, "before": before, "after": after}

    def classify(self, mail, local, courses=None, rules=None):
        courses = self.db.courses() if courses is None else courses
        rules = self.rules() if rules is None else rules
        text = " ".join([mail["subject"], mail["snippet"], mail["body"]])
        hits, reasons, categories, attention = set(), [], set(), []
        ignore, explicit_none, uncertain = False, False, False
        for course in courses:
            for name in filter(
                None, {course["name"], course.get("source_name"), *course.get("source_names", [])}
            ):
                if len(normalized(name)) >= 4 and matches(name, text):
                    hits.add(course["id"])
                    reasons.append(name)
        fields = mail_fields(mail)
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            hit = matches(rule["contains"], fields[rule["field"]])
            if rule["field"] in {"body", "any"} and not mail["body_complete"] and not hit:
                if rule["action"] in {"course", "none"}:
                    uncertain = True
                continue
            if not hit:
                continue
            priority_hit = rule.get("priority") and priority_match(rule, fields)
            if rule.get("priority") and rule["action"] == "category" and not priority_hit:
                continue
            reasons.append(rule["contains"])
            if priority_hit:
                attention.append(
                    rule["value"] if rule["action"] == "category" else rule["contains"]
                )
            if rule["action"] == "course":
                matched = next(
                    (
                        c
                        for c in courses
                        if rule["value"]
                        in (c["id"], c.get("workspace_id"), *c.get("source_course_ids", []))
                    ),
                    None,
                )
                if matched:
                    hits.add(matched["id"])
                else:
                    uncertain = True
            elif rule["action"] == "category":
                categories.add(rule["value"])
            elif rule["action"] == "none":
                explicit_none = True
            elif rule["action"] == "ignore":
                ignore = True
        # A missing body may contain another course; never infer a confident absence.
        uncertain = uncertain or not mail["body_complete"]
        ambiguous = len(hits) > 1 or (explicit_none and bool(hits)) or uncertain
        state = "unclassifiable" if ambiguous else "classified" if hits else "none"
        course_id = next(iter(hits)) if state == "classified" else None
        override = local.get("course_id", "auto")
        if override != "auto":
            resolved = next(
                (
                    c
                    for c in courses
                    if override in (c["id"], c.get("workspace_id"), *c.get("source_course_ids", []))
                ),
                None,
            )
            course_id = resolved["id"] if resolved else None
            state = "classified" if resolved else "none" if override == "none" else "unclassifiable"
        # Ambiguity never automatically removes a message from view.
        ignored = local.get("ignored", ignore and state != "unclassifiable")
        return {
            "classification": state,
            "course_id": course_id,
            "categories": sorted(categories),
            "classification_reason": (
                "Multiple courses match"
                if len(hits) > 1
                else "Email content incomplete"
                if uncertain
                else "Conflicting rules"
                if ambiguous
                else ""
            ),
            "matched_rules": list(dict.fromkeys(reasons)),
            "ignored": ignored,
            "attention": bool(attention),
            "attention_reasons": list(dict.fromkeys(attention)),
            "candidate_course_ids": sorted(hits) if state == "unclassifiable" else [],
        }

    def _view(self, row, detail=False, courses=None, rules=None):
        mail, local = json.loads(row["payload"]), json.loads(row["local_payload"])
        view = (
            mail
            | self.classify(mail, local, courses, rules)
            | {
                "deleted": local.get("deleted", False),
                "starred": local.get("starred", False),
                "unread": not local["read"] if "read" in local else mail["unread"],
                "course_override": local.get("course_id", "auto"),
            }
        )
        if view["course_override"] not in {"auto", "none"} and view["course_id"]:
            view["course_override"] = view["course_id"]
        if not detail:
            view.pop("body")
        return view

    def get(self, key):
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM mail_messages WHERE id=?", (key,)).fetchone()
            linked = [
                r[0] for r in conn.execute("SELECT id FROM custom_tasks WHERE email_id=?", (key,))
            ]
        if not row:
            raise KeyError(key)
        return self._view(row, True) | {"task_ids": linked}

    def list(
        self, deleted=False, ignored=False, offset=0, limit=50, attention_only=False, order="newest"
    ):
        courses, rules = self.db.courses(), self.rules()
        result = []
        with self.db.connection() as conn:
            for row in conn.execute(
                "SELECT * FROM mail_messages ORDER BY received_at DESC, id DESC"
            ):
                item = self._view(row, courses=courses, rules=rules)
                if (item["deleted"] and not deleted) or (item["ignored"] and not ignored):
                    continue
                if attention_only and not item["attention"]:
                    continue
                result.append(item)
        if order == "attention":
            result.sort(key=lambda item: not item["attention"])
        total = len(result)
        return {
            "messages": result[offset : offset + limit],
            "total": total,
            "has_more": total > offset + limit,
        }

    def preview_rule(self, value: MailRulePreview):
        courses, before_rules = self.db.courses(), self.rules(initialize=False)
        current = next((rule for rule in before_rules if rule["id"] == value.id), None)
        if value.operation in {"update", "delete"} and current is None:
            raise KeyError(value.id)
        proposed = None
        if value.operation in {"add", "update"}:
            if value.rule is None:
                raise ValueError("Enter a rule to preview")
            proposed = self._validated_rule(value.rule).model_dump() | {"id": value.id or "preview"}
        after_rules = [rule for rule in before_rules if rule["id"] != value.id]
        if value.operation == "reset-defaults":
            after_rules = [rule for rule in before_rules if rule["id"] not in DEFAULT_MAIL_RULES]
            after_rules.extend(
                rule.model_dump() | {"id": key} for key, rule in DEFAULT_MAIL_RULES.items()
            )
        elif proposed:
            after_rules.append(proposed)
        count = {
            key: 0
            for key in (
                "cached",
                "matched",
                "changed",
                "newly_ignored",
                "restored",
                "classification_changed",
                "category_changed",
                "attention_changed",
                "new_conflicts",
                "incomplete",
                "manual_overrides",
            )
        }
        rows = []
        keys = (
            "classification",
            "course_id",
            "categories",
            "ignored",
            "attention",
            "candidate_course_ids",
        )
        with self.db.connection() as conn:
            for row in conn.execute(
                "SELECT * FROM mail_messages ORDER BY received_at DESC, id DESC"
            ):
                mail, local = json.loads(row["payload"]), json.loads(row["local_payload"])
                before = self.classify(mail, local, courses, before_rules)
                after = self.classify(mail, local, courses, after_rules)
                changed = any(before[key] != after[key] for key in keys)
                fields = mail_fields(mail)
                hit = any(
                    rule and matches(rule["contains"], fields[rule["field"]])
                    for rule in (current, proposed)
                )
                count["cached"] += 1
                count["matched"] += bool(changed if value.operation == "reset-defaults" else hit)
                count["changed"] += changed
                count["newly_ignored"] += not before["ignored"] and after["ignored"]
                count["restored"] += before["ignored"] and not after["ignored"]
                count["classification_changed"] += (
                    before["classification"],
                    before["course_id"],
                ) != (after["classification"], after["course_id"])
                count["category_changed"] += before["categories"] != after["categories"]
                count["attention_changed"] += before["attention"] != after["attention"]
                count["new_conflicts"] += (
                    before["classification"] != "unclassifiable"
                    and after["classification"] == "unclassifiable"
                )
                count["incomplete"] += not mail["body_complete"]
                if hit or changed:
                    manual = local.get("course_id", "auto") != "auto" or "ignored" in local
                    count["manual_overrides"] += manual
                    rows.append(
                        {
                            "id": mail["id"],
                            "subject": mail["subject"],
                            "sender": mail["sender"],
                            "deleted": local.get("deleted", False),
                            "body_complete": mail["body_complete"],
                            "manual_override": manual,
                            "changed": changed,
                            "before": {key: before[key] for key in keys},
                            "after": {key: after[key] for key in keys},
                        }
                    )
        return {
            "counts": count,
            "messages": rows[value.offset : value.offset + value.limit],
            "total": len(rows),
            "has_more": len(rows) > value.offset + value.limit,
        }

    def patch(self, key, patch):
        if "course_id" in patch and patch["course_id"] not in {"auto", "none"}:
            course = resolve_course(self.db, patch["course_id"])
            patch["course_id"] = course.get("workspace_id") or course["id"]
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT local_payload FROM mail_messages WHERE id=?", (key,)
            ).fetchone()
            if not row:
                raise KeyError(key)
            conn.execute(
                "UPDATE mail_messages SET local_payload=? WHERE id=?",
                (json.dumps(json.loads(row[0]) | patch), key),
            )
        return self.get(key)


def build_mail_rules_router(db):
    router = APIRouter(prefix="/api/mail/rules")
    store = MailStore(db)

    @router.post("/preview")
    async def preview(value: MailRulePreview):
        try:
            return store.preview_rule(value)
        except KeyError as exc:
            raise HTTPException(404, "Rule not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
