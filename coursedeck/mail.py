"""Local mail, deterministic rules and independently owned to-dos. No AI required."""

import json
import re
import unicodedata
from typing import Literal
from uuid import uuid4

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


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def matches(needle, haystack):
    # Word boundaries prevent e.g. 'art' matching 'department'.
    return (
        re.search(r"(?<!\w)" + re.escape(normalized(needle)) + r"(?!\w)", normalized(haystack))
        is not None
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

    def rules(self):
        with self.db.connection() as conn:
            return [
                json.loads(r["payload"]) | {"id": r["id"]}
                for r in conn.execute("SELECT * FROM mail_rules ORDER BY rowid")
            ]

    def add_rule(self, rule):
        if rule.action == "course":
            course = resolve_course(self.db, rule.value)
            rule.value = course.get("workspace_id") or course["id"]
        if rule.action == "category" and not rule.value:
            raise ValueError("Enter a category")
        key = uuid4().hex
        with self.db.connection() as conn:
            conn.execute("INSERT INTO mail_rules VALUES (?, ?)", (key, rule.model_dump_json()))
        return key

    def delete_rule(self, key):
        with self.db.connection() as conn:
            if not conn.execute("DELETE FROM mail_rules WHERE id=?", (key,)).rowcount:
                raise KeyError(key)

    def classify(self, mail, local, courses=None, rules=None):
        courses = self.db.courses() if courses is None else courses
        rules = self.rules() if rules is None else rules
        text = " ".join([mail["subject"], mail["snippet"], mail["body"]])
        hits, reasons, categories = set(), [], set()
        ignore, explicit_none, uncertain = False, False, False
        for course in courses:
            for name in filter(
                None, {course["name"], course.get("source_name"), *course.get("source_names", [])}
            ):
                if len(normalized(name)) >= 4 and matches(name, text):
                    hits.add(course["id"])
                    reasons.append(name)
        for keyword, category in [("action required", "Action required"), ("survey", "Survey")]:
            if matches(keyword, text):
                categories.add(category)
        fields = {
            "sender": mail["sender_email"] or mail["sender"],
            "subject": mail["subject"],
            "body": mail["body"],
            "any": text + " " + mail["sender_email"],
        }
        for rule in rules:
            hit = matches(rule["contains"], fields[rule["field"]])
            if rule["field"] in {"body", "any"} and not mail["body_complete"] and not hit:
                if rule["action"] in {"course", "none"}:
                    uncertain = True
                continue
            if not hit:
                continue
            reasons.append(rule["contains"])
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

    def list(self, deleted=False, ignored=False, offset=0, limit=50):
        courses, rules = self.db.courses(), self.rules()
        result, total = [], 0
        with self.db.connection() as conn:
            for row in conn.execute(
                "SELECT * FROM mail_messages ORDER BY received_at DESC, id DESC"
            ):
                item = self._view(row, courses=courses, rules=rules)
                if (item["deleted"] and not deleted) or (item["ignored"] and not ignored):
                    continue
                if offset <= total < offset + limit:
                    result.append(item)
                total += 1
        return {"messages": result, "total": total, "has_more": total > offset + limit}

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
