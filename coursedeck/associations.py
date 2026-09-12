"""Local evidence links. A relation never merges source tasks or their statuses."""

import hashlib
import json
import re
from threading import RLock
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .domain import now
from .knowledge import safe_source_url
from .mail import MailStore
from .revisions import DatabaseRevision


def relation_url(value):
    try:
        parsed = urlsplit(value or "")
        if parsed.username is not None or parsed.password is not None:
            return None
        # Reject malformed ports before either rendering or matching the URL.
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return safe_source_url(value)


def source_identity(value):
    """Only assignment-level routes qualify; home/course URLs never imply identity."""
    value = relation_url(value)
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.port not in {None, 443 if parsed.scheme == "https" else 80}:
        return None
    host, path = parsed.hostname.lower(), parsed.path.rstrip("/")
    query = parse_qs(parsed.query)
    if host in {"gradescope.com", "www.gradescope.com"}:
        hit = re.fullmatch(
            r"/courses/([^/]+)/assignments/([^/]+)(?:/(?:submissions|review))?", path
        )
        if hit:
            return "gradescope", host.removeprefix("www."), *hit.groups()
    if (
        host in {"webassign.net", "www.webassign.net"}
        and path in {"/web/Student/Assignment-Responses/last", "/web/Student/Assignment-Responses"}
        and len(query.get("dep", [])) == 1
    ):
        return "webassign", query["dep"][0]
    if host == "classroom.google.com":
        hit = re.fullmatch(r"/(?:u/\d+/)?c/([^/]+)/a/([^/]+)(?:/details)?", path)
        if hit:
            return "google_classroom", *hit.groups()
    # A host alone cannot establish Brightspace provenance. Exact d2l route + IDs can.
    if path.startswith("/d2l/"):
        for route, keys in [
            ("/d2l/lms/dropbox/user/folder_submit_files.d2l", ("ou", "db")),
            ("/d2l/lms/quizzing/user/quiz_summary.d2l", ("ou", "qi")),
        ]:
            if path == route and all(len(query.get(key, [])) == 1 for key in keys):
                return "brightspace", host, route, *(query[key][0] for key in keys)
        hit = re.fullmatch(r"/d2l/le/content/([^/]+)/viewContent/([^/]+)/View", path)
        if hit:
            return "brightspace", host, "content", *hit.groups()
        hit = re.fullmatch(r"/d2l/le/content/([^/]+)/Home", path)
        identifiers = query.get("itemIdentifier", [])
        if hit and len(identifiers) == 1:
            item = re.fullmatch(
                r"D2L\.LE\.Content\.ContentObject\.(TopicCO|ModuleCO)-(\d+)", identifiers[0]
            )
            if item:
                kind = "content" if item[1] == "TopicCO" else "content_module"
                return "brightspace", host, kind, hit[1], item[2]
        hit = re.fullmatch(r"/d2l/le/([^/]+)/discussions/topics/([^/]+)/View", path)
        if hit:
            return "brightspace", host, "discussion", *hit.groups()
    return None


def explicit_links(text):
    text = text or ""
    urls = []
    if "<" in text:
        soup = BeautifulSoup(text, "html.parser")
        urls = [a.get("href", "") for a in soup.select("a[href]")]
        text = soup.get_text(" ")
    urls += re.findall(r"https?://[^\s<>\"']+", text)
    return {identity for url in urls if (identity := source_identity(url.rstrip(".,);]")))}


def assignment_numbers(text):
    # Dates, chapter numbers and generic title similarity are deliberately insufficient.
    return {
        match.group(1).casefold().replace(" ", "") + ":" + match.group(2).casefold()
        for match in re.finditer(
            r"\b(homework|hw|assignment|quiz|lab|project|problem\s*set|pset)\s*[#:]?\s*"
            r"(\d+(?:[.-]\d+)*(?:[a-z])?)(?![\w.-])",
            (text or "").replace("_", " "),
            re.I,
        )
    }


def _numbers(text):
    return {
        token.replace("hw:", "homework:").replace("pset:", "problemset:")
        for token in assignment_numbers(text)
    }


def stable_references(text):
    return {
        (match.group(1).casefold(), match.group(2))
        for match in re.finditer(
            r"\b(gradescope|webassign)\s+assignment\s+id\s*[:#=]?\s*"
            r"([A-Za-z0-9_-]+)(?![\w-])",
            text or "",
            re.I,
        )
    }


def _pair(left, right):
    left, right = sorted((left, right))
    key = hashlib.sha256(json.dumps([left, right]).encode()).hexdigest()[:32]
    return key, left, right


class AssociationStore:
    def __init__(self, db, knowledge_store, mail_store=None):
        self.db, self.knowledge = db, knowledge_store
        self.mail = mail_store or MailStore(db)
        self._lock = RLock()
        self._task_revision = DatabaseRevision(db.path)
        self._knowledge_revision = DatabaseRevision(knowledge_store.path)
        self._cached_revision = None
        self._cached_inventory = None

    def close(self):
        with self._lock:
            self._task_revision.close()
            self._knowledge_revision.close()
            self._cached_revision = self._cached_inventory = None

    def _revision(self):
        return self._task_revision.token(), self._knowledge_revision.token()

    def linked_document_ids(self, task_id):
        """Read only current document links; chat must not implicitly scan mailbox bodies."""
        task = next((item for item in self.db.tasks() if item["id"] == task_id), None)
        if not task:
            return set()
        course_id = self.db.resolve_course_alias(task.get("course_id"))
        enabled = {
            course["id"]
            for course in self.db.courses()
            if not course.get("disabled") and not course.get("deleted")
        }
        if not course_id or course_id not in enabled:
            return set()
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_relations WHERE "
                "(left_kind='task' AND left_id=? AND right_kind='document') OR "
                "(right_kind='task' AND right_id=? AND left_kind='document')",
                (task_id, task_id),
            ).fetchall()
        decisions = {
            row["right_id"] if row["left_kind"] == "task" else row["left_id"]: row for row in rows
        }
        result = set()
        with self.knowledge.connection() as conn:
            for document in conn.execute(
                "SELECT id,course_id,source_task_id FROM documents WHERE kind != 'assignment'"
            ):
                if self.db.resolve_course_alias(document["course_id"]) != course_id:
                    continue
                decision = decisions.get(document["id"])
                if decision and decision["state"] == "rejected":
                    continue
                if (
                    decision
                    and decision["active"]
                    and decision["state"] in {"automatic", "confirmed"}
                ) or document["source_task_id"] == task_id:
                    result.add(document["id"])
        return result

    def _inventory(self):
        courses = self.db.courses()
        active_courses = {
            c["id"] for c in courses if not c.get("disabled") and not c.get("deleted")
        }
        aliases = {}

        def scope(key):
            if key not in aliases:
                aliases[key] = self.db.resolve_course_alias(key) if key else None
            return aliases[key]

        records = {}

        def add(kind, item):
            course = scope(item.get("course_id"))
            title = item.get("title", item.get("subject", ""))
            status = item.get("submission_status", "")
            body = item.get("description", item.get("body", ""))
            if kind == "mail" and item.get("body_stale"):
                # Retained text is available in Inbox, but cannot establish a new
                # current association after the Gmail thread has changed.
                body = ""
            if kind == "mail":
                status = (
                    "Deleted"
                    if item.get("deleted")
                    else "Ignored"
                    if item.get("ignored")
                    else "Unread"
                    if item.get("unread")
                    else "Read"
                )
            elif kind == "document":
                status = "Material" if item.get("complete") is True else "Incomplete"
            elif item.get("source_availability") in {"missing", "unconfirmed"}:
                status = item["source_availability"]
            elif item.get("source_status_known") is False:
                status = "unknown"
            records[(kind, item["id"])] = {
                "kind": kind,
                "id": item["id"],
                "title": title,
                "provider": item.get("provider", "gmail"),
                "course_id": course,
                "url": relation_url(item.get("url")),
                "status": status or "unknown",
                "enabled": course in active_courses if course else kind != "document",
                "complete": item.get("complete", item.get("body_complete")),
                **(
                    {
                        "body_stale": bool(item.get("body_stale")),
                        "body_checked_at": item.get("body_checked_at"),
                    }
                    if kind == "mail"
                    else {}
                ),
                "source_modified_at": item.get("source_modified_at"),
                "fetched_at": item.get("fetched_at"),
                "_body": body + "\n" + item.get("snippet", ""),
                "_source_task_id": item.get("source_task_id"),
                "_email_id": item.get("email_id"),
                "_stable_id": (item.get("provider"), item.get("external_id")),
            }

        for task in self.db.tasks():
            add("task", task)
        rules = self.mail.rules()
        with self.db.connection() as conn:
            for row in conn.execute("SELECT * FROM mail_messages"):
                add("mail", self.mail._view(row, True, courses, rules))
        with self.knowledge.connection() as conn:
            for row in conn.execute("SELECT * FROM documents WHERE kind != 'assignment'"):
                add("document", self.knowledge._document(row))
        return records

    @staticmethod
    def _public(item):
        return {key: val for key, val in item.items() if not key.startswith("_")}

    @staticmethod
    def _scope_ok(left, right, manual=False):
        if not left["enabled"] or not right["enabled"]:
            return False
        if manual and (left["course_id"] is None or right["course_id"] is None):
            return True
        return bool(left["course_id"] and left["course_id"] == right["course_id"])

    def rebuild(self):
        with self._lock:
            before = self._revision()
            if before == self._cached_revision:
                return self._cached_inventory
            records = self._rebuild()
            after = self._revision()
            # A write (ours or concurrent) invalidates this pass. The next stable
            # pass establishes the cache; never bless a possibly mixed snapshot.
            self._cached_revision = before if before == after else None
            self._cached_inventory = records
            return records

    def _rebuild(self):
        records = self._inventory()
        proposed = {}
        # Precompute identity and link sets once; never compare fuzzy titles.
        identities = {key: source_identity(item["url"]) for key, item in records.items()}
        links = {key: explicit_links(item["_body"]) for key, item in records.items()}
        stable_links = {key: stable_references(item["_body"]) for key, item in records.items()}
        numbers = {
            key: _numbers(item["title"] + "\n" + item["_body"]) for key, item in records.items()
        }
        identity_counts = {}
        stable_counts = {}
        for key, identity in identities.items():
            if key[0] == "task":
                if identity:
                    bucket = (records[key]["course_id"], identity)
                    identity_counts[bucket] = identity_counts.get(bucket, 0) + 1
                stable_bucket = (records[key]["course_id"], records[key]["_stable_id"])
                stable_counts[stable_bucket] = stable_counts.get(stable_bucket, 0) + 1
        by_course = {}
        for key, item in records.items():
            if item["enabled"] and item["course_id"]:
                by_course.setdefault(item["course_id"], []).append(key)
        # A unique explicit assignment reference can relate an unclassified email
        # without rewriting the user's independent email classification.
        unscoped_mail = {}
        for key, item in records.items():
            if key[0] != "mail" or item["course_id"] is not None:
                continue
            hits = [
                task_key
                for task_key, task_item in records.items()
                if task_key[0] == "task"
                and (
                    identities[task_key]
                    and identities[task_key] in links[key]
                    or task_item["_stable_id"] in stable_links[key]
                )
            ]
            if len(hits) == 1:
                unscoped_mail.setdefault(hits[0], []).append(key)
        for left, task in records.items():
            if left[0] != "task":
                continue
            for right in by_course.get(task["course_id"], []) + unscoped_mail.get(left, []):
                unique_mail = right in unscoped_mail.get(left, [])
                if left == right or not self._scope_ok(task, records[right], unique_mail):
                    continue
                item = records[right]
                key, a, b = _pair(left, right)
                explicit = (
                    identities[left]
                    and identities[left] in links[right]
                    or identities[right]
                    and identities[right] in links[left]
                    or identities[left]
                    and identities[left] == identities[right]
                )
                stable = task["_stable_id"] in stable_links[right] or (
                    right[0] == "task" and item["_stable_id"] in stable_links[left]
                )
                explicit = explicit or stable
                direct = item["_source_task_id"] == task["id"] or (
                    right[0] == "mail" and task["_email_id"] == item["id"]
                )
                overlap = numbers[left] & numbers[right]
                ambiguous = any(
                    identity and identity_counts.get((task["course_id"], identity), 0) > 1
                    for identity in (identities[left], identities[right])
                )
                ambiguous = ambiguous or bool(
                    stable
                    and any(
                        stable_counts.get((task["course_id"], value["_stable_id"]), 0) > 1
                        for value in (task, item)
                    )
                )
                attempts = [
                    re.findall(r"\b(?:attempt|version)\s*#?\s*(\d+)\b", value["title"], re.I)
                    for value in (task, item)
                ]
                ambiguous = ambiguous or bool(any(attempts) and attempts[0] != attempts[1])
                if explicit and ambiguous and not direct:
                    state, evidence = (
                        "suggested",
                        [
                            {
                                "kind": "ambiguous_reference",
                                "label": "Source reference has multiple items or attempts",
                            }
                        ],
                    )
                elif explicit or direct:
                    state, evidence = (
                        "automatic",
                        [
                            {
                                "kind": "source_id" if direct or stable else "source_url",
                                "label": "Explicit source reference",
                            }
                        ],
                    )
                elif overlap:
                    state, evidence = (
                        "suggested",
                        [
                            {
                                "kind": "assignment_number",
                                "label": "Same course and " + ", ".join(sorted(overlap)),
                            }
                        ],
                    )
                else:
                    continue
                proposed[key] = (a, b, state, evidence)
        stamp = now().isoformat()
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM task_relations")
            }
            for key, (a, b, state, evidence) in proposed.items():
                old = previous.get(key)
                if old and old["state"] in {"confirmed", "rejected"}:
                    state = old["state"]
                active = state != "rejected"
                encoded = json.dumps(evidence)
                if old and (old["state"], old["active"], old["evidence"]) == (
                    state,
                    active,
                    encoded,
                ):
                    continue
                conn.execute(
                    "INSERT INTO task_relations VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state,active=excluded.active,"
                    "evidence=excluded.evidence,updated_at=excluded.updated_at",
                    (key, *a, *b, state, active, encoded, stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO task_relation_history(relation_id,action,created_at) "
                    "VALUES (?,?,?)",
                    (key, "detected" if not old else "evidence_updated", stamp),
                )
            for key, old in previous.items():
                if key in proposed:
                    continue
                a = records.get((old["left_kind"], old["left_id"]))
                b = records.get((old["right_kind"], old["right_id"]))
                active = bool(
                    old["state"] == "confirmed" and a and b and self._scope_ok(a, b, True)
                )
                if old["active"] != active:
                    conn.execute(
                        "UPDATE task_relations SET active=?,updated_at=? WHERE id=?",
                        (active, stamp, key),
                    )
                    conn.execute(
                        "INSERT INTO task_relation_history(relation_id,action,created_at) "
                        "VALUES (?,?,?)",
                        (key, "restored" if active else "inactive", stamp),
                    )
        return records

    def view(self, task_id, include_rejected=False):
        records = self.rebuild()
        task = records.get(("task", task_id))
        if not task:
            raise KeyError(task_id)
        relations = []
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_relations WHERE (left_kind='task' AND left_id=?) "
                "OR (right_kind='task' AND right_id=?) ORDER BY created_at,id",
                (task_id, task_id),
            ).fetchall()
            for row in rows:
                if row["state"] == "rejected" and not include_rejected:
                    continue
                other = (
                    (row["right_kind"], row["right_id"])
                    if (row["left_kind"], row["left_id"]) == ("task", task_id)
                    else (row["left_kind"], row["left_id"])
                )
                target = records.get(other)
                relations.append(
                    {
                        "id": row["id"],
                        "state": row["state"],
                        "active": bool(row["active"]),
                        "evidence": json.loads(row["evidence"]),
                        "target": self._public(target)
                        if target
                        else {
                            "kind": other[0],
                            "id": other[1],
                            "title": "Unavailable item",
                            "status": "Unavailable",
                            "url": None,
                        },
                        "history": [
                            dict(event)
                            for event in conn.execute(
                                "SELECT action,created_at FROM task_relation_history "
                                "WHERE relation_id=? ORDER BY id",
                                (row["id"],),
                            )
                        ],
                    }
                )
        return {
            "task_id": task_id,
            "relations": relations,
            "available": [
                self._public(item)
                for key, item in records.items()
                if key != ("task", task_id) and self._scope_ok(task, item, True)
            ],
        }

    def decide(self, task_id, target_kind, target_id, action):
        records = self.rebuild()
        left, right = ("task", task_id), (target_kind, target_id)
        if left not in records or (right not in records and action not in {"unlink", "reject"}):
            raise KeyError("Item not found")
        if left == right:
            raise ValueError("Cannot link a task to itself")
        if action not in {"link", "unlink", "confirm", "reject"}:
            raise ValueError("Unknown relation action")
        if action in {"link", "confirm"} and not self._scope_ok(
            records[left], records[right], True
        ):
            raise ValueError("Items must belong to the same enabled course")
        key, a, b = _pair(left, right)
        stamp = now().isoformat()
        state = "confirmed" if action in {"link", "confirm"} else "rejected"
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM task_relations WHERE id=?", (key,)).fetchone()
            if action in {"confirm", "reject", "unlink"} and not old:
                raise ValueError("Relation not found")
            evidence = (
                old["evidence"]
                if old
                else json.dumps([{"kind": "manual", "label": "Linked by you"}])
            )
            if not old or old["state"] != state or bool(old["active"]) != (state == "confirmed"):
                conn.execute(
                    "INSERT INTO task_relations VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state,active=excluded.active,"
                    "updated_at=excluded.updated_at",
                    (key, *a, *b, state, state == "confirmed", evidence, stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO task_relation_history(relation_id,action,created_at) "
                    "VALUES (?,?,?)",
                    (key, action, stamp),
                )
        return self.view(task_id, True)


class RelationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_kind: Literal["task", "mail", "document"]
    target_id: str = Field(min_length=1, max_length=2000)
    action: Literal["link", "unlink", "confirm", "reject"]


def build_router(store):
    router = APIRouter(prefix="/api/associations")

    @router.get("/task/{task_id:path}")
    def relations(task_id: str, include_rejected: bool = False):
        try:
            return store.view(task_id, include_rejected)
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc

    @router.post("/task/{task_id:path}")
    def decide(task_id: str, value: RelationAction):
        try:
            return store.decide(task_id, **value.model_dump())
        except KeyError as exc:
            raise HTTPException(404, "Item not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
