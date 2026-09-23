"""Cached Inbox reads and local-only edits, bound to the current user instruction."""

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .knowledge import safe_source_url
from .mail import MailStore

QUOTED = r'["“]([^"”]+)["”]'
EXAMPLES = [
    "查找 2026-07-31 之前的邮件并预览删除清单",
    "恢复邮件“邮件标题或 ID”",
    "把邮件“邮件标题或 ID”标为已读",
    "把邮件“邮件标题或 ID”标为未读",
    "把所有未读邮件标为已读",
    "查找标题包含“newsletter”的邮件",
    "查找发件人为“sender@example.edu”的邮件",
]


def inbox_intent(message):
    text = re.sub(r"^(?:请(?:帮我)?|帮我|please\s+)", "", message.strip(), flags=re.I)
    text = text.rstrip("。.!！").strip()
    action, target = None, None
    match = re.fullmatch(r"(删除|恢复)\s*(.+)", text)
    if match:
        action, target = ("delete" if match[1] == "删除" else "restore"), match[2]
    match = re.fullmatch(r"(?:把|将)?(.+?)(?:标为|标记为|设为)(已读|未读)", text)
    if match:
        target, action = match[1], "read" if match[2] == "已读" else "unread"
    match = re.fullmatch(r"(delete|restore)\s+(.+)", text, re.I)
    if match:
        action, target = match[1].lower(), match[2]
    match = re.fullmatch(r"mark\s+(.+?)\s+(?:as\s+)?(read|unread)", text, re.I)
    if match:
        target, action = match[1], match[2].lower()
    if not action:
        return None
    target = target.strip()
    single = re.fullmatch(r"(?:邮件|email|mail)\s*" + QUOTED, target, re.I)
    if single:
        return {"action": action, "field": "exact", "value": single[1], "bulk": False}
    batch = re.fullmatch(r"(?:标题包含|发件人为)\s*" + QUOTED + r"的所有邮件", target)
    if batch:
        return {
            "action": action,
            "field": "subject" if target.startswith("标题") else "sender",
            "value": batch[1],
            "bulk": True,
        }
    batch = re.fullmatch(
        r"all (?:emails|mail) (?:with subject containing|from)\s*" + QUOTED, target, re.I
    )
    if batch:
        return {
            "action": action,
            "field": "subject" if "subject containing" in target.lower() else "sender",
            "value": batch[1],
            "bulk": True,
        }
    states = {
        "所有邮件": "all",
        "全部邮件": "all",
        "收件箱所有邮件": "all",
        "所有未读邮件": "unread",
        "所有已读邮件": "read",
        "已自动忽略的邮件": "ignored",
        "所有已忽略邮件": "ignored",
        "所有已删除邮件": "deleted",
        "all emails": "all",
        "all mail": "all",
        "all unread emails": "unread",
        "all read emails": "read",
        "all ignored emails": "ignored",
        "all deleted emails": "deleted",
    }
    if target.lower() in states:
        return {"action": action, "field": states[target.lower()], "value": "", "bulk": True}
    return None


class InboxTools:
    def __init__(self, service, value, expose):
        self.service, self.store, self.value = service, MailStore(service.db), value
        self.expose = expose
        self.enabled = True
        self.intent = inbox_intent(value.message)
        self.preview = self.receipt = None
        self.seen = set()

    def definitions(self, define):
        paging = {
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }
        result = [
            define(
                "search_inbox",
                "Search cached CourseDeck mail. No Gmail requests. "
                "Returns IDs, subjects, senders and local states. Page all matches; "
                "ambiguous subjects need an exact ID. A selected course limits results. "
                "before excludes that date; after includes that date, at local midnight. "
                "Dates require YYYY-MM-DD; clarify missing years. sender is an exact email "
                "address or display name; subject matches a substring. Missing received dates "
                "are reported and excluded when a date filter is used.",
                {
                    "query": {"type": "string", "maxLength": 2000},
                    **paging,
                    "include_deleted": {"type": "boolean"},
                    "include_ignored": {"type": "boolean"},
                    "before": {"type": "string", "format": "date"},
                    "after": {"type": "string", "format": "date"},
                    "sender": {"type": "string", "maxLength": 1000},
                    "subject": {"type": "string", "maxLength": 2000},
                },
                [],
            ),
            define(
                "read_inbox_mail",
                "Read cached mail using an ID from search_inbox. "
                "Does not mark it read or contact Gmail. Report incomplete/stale bodies.",
                {"mail_id": {"type": "string"}, "offset": paging["offset"]},
                ["mail_id"],
            ),
        ]
        if self.intent and self.intent["action"] != "delete":
            result += [
                define(
                    "preview_inbox_edit",
                    "Preview the exact local Inbox edit bound "
                    "to the user message. Does not write. Returns matching count and "
                    "version. Ordinary batches exclude deleted/ignored mail unless named.",
                    {},
                    [],
                ),
                define(
                    "apply_inbox_edit",
                    "Apply only the previewed local Inbox change. "
                    "Gmail is never contacted. Use the preview version; on conflict "
                    "re-preview. Restore shows locally deleted mail again.",
                    {"expected_version": {"type": "string", "minLength": 64, "maxLength": 64}},
                    ["expected_version"],
                ),
            ]
        return result

    def _course_scope(self):
        courses = self.service.db.courses()
        rules = self.store.rules(initialize=False)
        cid = self.service.canonical_course(self.value.course_id) if self.value.course_id else None
        course = (
            next((c for c in courses if cid in (c["id"], c.get("workspace_id"))), None)
            if cid
            else None
        )
        if cid and not course:
            raise ValueError("Selected course is no longer available")
        return courses, rules, course["id"] if course else None

    def rows(self, conn, *, detail=False, mail_id=None):
        courses, rules, cid = self._course_scope()
        result = []
        query = "SELECT * FROM mail_messages"
        args = ()
        if mail_id is not None:
            query += " WHERE id=?"
            args = (mail_id,)
        for row in conn.execute(query + " ORDER BY received_at DESC, id DESC", args):
            view = self.store._view(row, detail=detail, courses=courses, rules=rules)
            if cid and view["course_id"] != cid:
                continue
            result.append((row, view))
        return result

    def _search_filters(self, args):
        allowed = {
            "query",
            "offset",
            "limit",
            "include_deleted",
            "include_ignored",
            "before",
            "after",
            "sender",
            "subject",
        }
        if not isinstance(args, dict) or args.keys() - allowed:
            raise ValueError("Invalid Inbox search")
        values = {"query": "", "offset": 0, "limit": 20, **args}
        if (
            any(
                not isinstance(values[key], str) or len(values[key]) > maximum
                for key, maximum in (("query", 2000), ("sender", 1000), ("subject", 2000))
                if key in values
            )
            or type(values["offset"]) is not int
            or values["offset"] < 0
            or type(values["limit"]) is not int
            or not 1 <= values["limit"] <= 50
            or any(
                type(values[key]) is not bool
                for key in ("include_deleted", "include_ignored")
                if key in values
            )
        ):
            raise ValueError("Invalid Inbox search")
        timezone = ZoneInfo(self.service.db.settings().timezone)
        bounds = {}
        for key in ("before", "after"):
            if key not in values:
                continue
            raw = values[key]
            if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                raise ValueError("Use a complete YYYY-MM-DD date; clarify a missing year")
            try:
                bounds[key] = datetime.combine(date.fromisoformat(raw), time.min, timezone)
            except ValueError as exc:
                raise ValueError("Use a valid YYYY-MM-DD date") from exc
        if "before" in bounds and "after" in bounds and bounds["after"] >= bounds["before"]:
            raise ValueError("after must be earlier than the exclusive before date")
        return values, bounds

    def _matching(self, args, *, connection=None):
        values, bounds = self._search_filters(args)
        courses, rules, cid = self._course_scope()
        terms = values["query"].casefold().split()
        sender = values.get("sender", "").strip().casefold()
        subject = values.get("subject", "").casefold()
        matches, missing_dates = [], 0
        with (
            nullcontext(connection) if connection is not None else self.service.db.connection()
        ) as conn:
            # Project searchable metadata first. Only candidates need full cached bodies
            # for the existing classification and ignore rules.
            candidates = conn.execute(
                "SELECT id,received_at,local_payload,"
                "json_extract(payload,'$.received_at') AS source_received_at,"
                "json_extract(payload,'$.subject') AS subject,"
                "json_extract(payload,'$.sender') AS sender,"
                "json_extract(payload,'$.sender_email') AS sender_email,"
                "json_extract(payload,'$.snippet') AS snippet "
                "FROM mail_messages ORDER BY received_at DESC,id DESC"
            )
            for candidate in candidates:
                local = json.loads(candidate["local_payload"])
                if local.get("deleted", False) and not values.get("include_deleted", False):
                    continue
                if local.get("ignored") is True and not values.get("include_ignored", False):
                    continue
                fields = {
                    key: str(candidate[key] or "").casefold()
                    for key in ("subject", "sender", "sender_email", "snippet")
                }
                if sender and sender not in {fields["sender"], fields["sender_email"]}:
                    continue
                if subject and subject not in fields["subject"]:
                    continue
                haystack = " ".join(fields.values())
                if not all(term in haystack for term in terms):
                    continue
                try:
                    received = datetime.fromisoformat(
                        candidate["source_received_at"].replace("Z", "+00:00")
                    )
                    if received.tzinfo is None:
                        received = None
                except (ValueError, AttributeError):
                    received = None
                if received is not None and (
                    "before" in bounds
                    and received >= bounds["before"]
                    or "after" in bounds
                    and received < bounds["after"]
                ):
                    continue
                row = conn.execute(
                    "SELECT * FROM mail_messages WHERE id=?", (candidate["id"],)
                ).fetchone()
                view = self.store._view(row, detail=False, courses=courses, rules=rules)
                if cid and view["course_id"] != cid:
                    continue
                if view["ignored"] and not values.get("include_ignored", False):
                    continue
                if received is None:
                    missing_dates += 1
                    if bounds:
                        continue
                matches.append(view)
        return matches, missing_dates, values

    def matching(self, args, *, connection=None):
        """Return all scoped matches without pagination or requiring earlier seen IDs."""
        return self._matching(args, connection=connection)[0]

    def search(self, args, *, connection=None):
        rows, missing_dates, values = self._matching(args, connection=connection)
        offset, limit = values["offset"], values["limit"]
        page = rows[offset : offset + limit]
        self.seen.update(mail["id"] for mail in page)
        return {
            "messages": [self.summary(mail) for mail in page],
            "total": len(rows),
            "has_more": offset + limit < len(rows),
            "offset": offset,
            "scope": self.value.course_id or "CourseDeck Inbox",
            "timezone": self.service.db.settings().timezone,
            "missing_date_count": missing_dates,
            "warnings": [
                f"{missing_dates} matching cached emails have no reliable received date "
                "and were excluded from date filtering"
            ]
            if missing_dates and ("before" in values or "after" in values)
            else [],
            "edit_available": bool(self.intent and self.intent["action"] != "delete"),
            "examples": EXAMPLES,
            "coverage": "Cached mail only; not a live Gmail check",
        }

    @staticmethod
    def summary(mail):
        return {
            key: mail.get(key)
            for key in (
                "id",
                "subject",
                "sender",
                "sender_email",
                "received_at",
                "course_id",
                "classification",
                "deleted",
                "ignored",
                "unread",
                "starred",
                "body_complete",
                "body_stale",
            )
        }

    def prepared(self, conn):
        if not self.intent:
            raise ValueError(
                "An explicit Inbox edit is required; clarify using search_inbox examples"
            )
        intent, matches = self.intent, []
        field, wanted = intent["field"], intent["value"].casefold()
        for row, view in self.rows(conn):
            if field == "exact":
                hit = wanted in {view["id"].casefold(), view["subject"].casefold()}
            else:
                deleted = intent["action"] == "restore" or field == "deleted"
                if view["deleted"] != deleted or (view["ignored"] and field != "ignored"):
                    continue
                hit = (
                    field in {"all", "deleted"}
                    or field == "ignored"
                    and view["ignored"]
                    or field == "read"
                    and not view["unread"]
                    or field == "unread"
                    and view["unread"]
                    or field == "subject"
                    and wanted in view["subject"].casefold()
                    or field == "sender"
                    and wanted in {view["sender_email"].casefold(), view["sender"].casefold()}
                )
            if hit:
                matches.append((row, view))
        if field == "exact" and len(matches) > 1:
            raise ValueError(
                "Multiple emails match this subject; specify a mail ID from search_inbox"
            )
        if len(matches) > 2000:
            raise ValueError("More than 2000 emails match; narrow the sender or subject filter")
        patch = (
            {"deleted": intent["action"] == "delete"}
            if intent["action"] in {"delete", "restore"}
            else {"read": intent["action"] == "read"}
        )
        version = hashlib.sha256(
            json.dumps(
                {
                    "intent": intent,
                    "course_id": self.value.course_id,
                    "rows": [
                        [row["id"], row["payload"], row["local_payload"], view["course_id"]]
                        for row, view in matches
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        count = sum(
            any(json.loads(row["local_payload"]).get(k) != v for k, v in patch.items())
            for row, _ in matches
        )
        return matches, patch, version, count

    async def run(self, name, args):
        if (
            name in {"preview_inbox_edit", "apply_inbox_edit"}
            and self.intent
            and self.intent["action"] == "delete"
        ):
            raise ValueError("Use preview_mail_deletion; deleting mail requires user confirmation")
        if name == "search_inbox":
            return self.search(args)
        if name == "read_inbox_mail":
            if args.keys() - {"mail_id", "offset"} or args.get("mail_id") not in self.seen:
                raise ValueError("Choose a mail ID returned by search_inbox in this question")
            offset = args.get("offset", 0)
            if type(offset) is not int or offset < 0:
                raise ValueError("Invalid mail excerpt offset")
            with self.service.db.connection() as conn:
                mail = next(
                    (v for _, v in self.rows(conn, detail=True, mail_id=args["mail_id"])), None
                )
            if mail is None:
                raise ValueError("Mail is no longer available in this conversation")
            warnings = (
                ["Cached email body is incomplete or stale"]
                if not mail["body_complete"] or mail.get("body_stale")
                else []
            )
            document = {
                "id": "mail:" + mail["id"],
                "title": mail["subject"],
                "kind": "email",
                "provider": "gmail",
                "course_id": mail["course_id"],
                "body": mail["body"],
                "url": safe_source_url(mail.get("url")),
                "checked_at": mail.get("body_checked_at"),
                "complete": not warnings,
                "warnings": warnings,
                "evidence": "cached_mail",
            }
            docs = self.expose([document], offset=offset)
            if not docs:
                raise ValueError("Evidence limit reached; continue in another question")
            return {"message": self.summary(mail), "documents": docs}
        if name == "preview_inbox_edit":
            if self.receipt is not None:
                raise ValueError("This Inbox edit already completed; use its recorded result")
            if args:
                raise ValueError(
                    "Inbox edit is bound to the user message; extra fields are forbidden"
                )
            with self.service.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                matches, patch, version, changed = self.prepared(conn)
            self.preview = version
            return {
                "action": self.intent["action"],
                "patch": patch,
                "matched": len(matches),
                "changed": changed,
                "version": version,
                "preview_only": True,
                "messages": [self.summary(v) for _, v in matches[:20]],
                "has_more": len(matches) > 20,
                "scope": self.value.course_id or "CourseDeck Inbox",
            }
        if name == "apply_inbox_edit":
            if (
                set(args) != {"expected_version"}
                or not self.preview
                or args["expected_version"] != self.preview
            ):
                raise ValueError("Preview this Inbox edit first and use its current version")
            if self.receipt is not None:
                return self.receipt
            with self.service.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                matches, patch, version, changed = self.prepared(conn)
                if version != self.preview:
                    raise ValueError("Matching emails or local states changed; preview again")
                items = []
                for row, view in matches:
                    before, after = self.store.patch_local(conn, row["id"], patch)
                    if len(items) < 20:
                        items.append(
                            {
                                "id": row["id"],
                                "subject": view["subject"],
                                "before": before,
                                "after": after,
                            }
                        )
            self.receipt = {
                "action": self.intent["action"],
                "matched": len(matches),
                "changed": changed,
                "messages": items,
                "has_more": len(matches) > 20,
                "scope": "CourseDeck only",
            }
            return self.receipt
        raise ValueError("Unsupported Inbox tool")
