"""Local source evidence and conversation storage, independent of the task database."""

import json
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from .domain import now
from .migrations import migrate
from .revisions import DatabaseRevision
from .task_state import unread_fields


def normalized_text(value):
    return unicodedata.normalize("NFKC", value or "").casefold()


def search_terms(query):
    words = re.findall(
        r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+(?:[-_][^\W_\u3400-\u9fff]+)*",
        normalized_text(query),
    )
    terms = {}
    for word in words[:32]:
        terms[word] = 1.0
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 2:
            for index in range(min(len(word) - 1, 64)):
                terms.setdefault(word[index : index + 2], 0.35)
    return dict(list(terms.items())[:96])


def material_freshness(document, current=None):
    # Checking a failed page does not refresh the body that the user is reading.
    value = document.get("fetched_at")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (ValueError, AttributeError):
        stamp = None
    if stamp is None or stamp.tzinfo is None:
        return "unknown"
    return "recent" if (current or now()) - stamp <= timedelta(days=7) else "stale"


def search_excerpt(body, query, limit=4000):
    normalized = normalized_text(body)
    positions = [normalized.find(term) for term in search_terms(query)]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - 120) if positions else 0
    # Unicode normalization can alter offsets; the original body is always retained.
    start = min(start, len(body))
    return body[start : start + limit], start


def safe_source_url(value):
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            return None
        forbidden = re.compile(r"token|secret|pass|auth|session|signature|saml|credential", re.I)
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not forbidden.search(key) and key.casefold() not in {"code", "state", "ticket"}
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except (TypeError, ValueError):
        return None


class KnowledgeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._revision = DatabaseRevision(self.path)
        self._search_lock = RLock()
        self._search_cache = None
        self._index_lock = RLock()
        self._task_revision = None
        self._indexed_revision = None
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            migrate(db, "knowledge")

    def close(self):
        with self._index_lock, self._search_lock:
            self._search_cache = None
            self._indexed_revision = None
            if self._task_revision is not None:
                self._task_revision.close()
            self._revision.close()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert_documents(self, documents: list[dict]):
        with self.connection() as db:
            changed = db.total_changes
            for document in documents:
                if not all(document.get(key) for key in ("id", "provider", "course_id", "title")):
                    raise ValueError("Document identity, course and title are required")
                if document.get("complete") is False and not document.get("body"):
                    previous = db.execute(
                        "SELECT * FROM documents WHERE id=?", (document["id"],)
                    ).fetchone()
                    if previous and previous["body"]:
                        old = self._document(previous)
                        document = document | {
                            "body": old["body"],
                            "updated_at": old["updated_at"],
                            "source_modified_at": old.get("source_modified_at"),
                            "fetched_at": old.get("fetched_at"),
                            "checked_at": document.get("checked_at") or now().isoformat(),
                            "warnings": list(
                                dict.fromkeys(
                                    document.get("warnings", [])
                                    + ["Latest body unreadable; previous body retained"]
                                )
                            ),
                        }
                db.execute(
                    """INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,
                    course_id=excluded.course_id, title=excluded.title, body=excluded.body,
                    url=excluded.url, kind=excluded.kind, updated_at=excluded.updated_at,
                    source_task_id=excluded.source_task_id, metadata=excluded.metadata
                    WHERE documents.provider IS NOT excluded.provider
                       OR documents.course_id IS NOT excluded.course_id
                       OR documents.title IS NOT excluded.title
                       OR documents.body IS NOT excluded.body
                       OR documents.url IS NOT excluded.url
                       OR documents.kind IS NOT excluded.kind
                       OR documents.updated_at IS NOT excluded.updated_at
                       OR documents.source_task_id IS NOT excluded.source_task_id
                       OR documents.metadata IS NOT excluded.metadata""",
                    (
                        document["id"],
                        document["provider"],
                        document["course_id"],
                        document["title"],
                        document.get("body", ""),
                        safe_source_url(document.get("url")),
                        document.get("kind", "material"),
                        document.get("updated_at") or now().isoformat(),
                        document.get("source_task_id"),
                        json.dumps(
                            {
                                key: document[key]
                                for key in (
                                    "source_modified_at",
                                    "complete",
                                    "evidence",
                                    "warnings",
                                    "fetched_at",
                                    "checked_at",
                                    "source_fields",
                                )
                                if key in document
                            },
                            sort_keys=True,
                        ),
                    ),
                )
            return db.total_changes - changed

    def get(self, document_id: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return self._document(row) if row else None

    @staticmethod
    def _document(row):
        result = dict(row)
        metadata = json.loads(result.pop("metadata", "{}"))
        return result | metadata

    def index_tasks(self, db):
        with self._index_lock:
            if self._task_revision is None or self._task_revision.path != db.path:
                if self._task_revision is not None:
                    self._task_revision.close()
                self._task_revision = DatabaseRevision(db.path)
                self._indexed_revision = None
            before = self._task_revision.token(), self._revision.token()
            if before == self._indexed_revision:
                return 0
            changed = self._index_tasks(db)
            after = self._task_revision.token(), self._revision.token()
            self._indexed_revision = before if before == after else None
            return changed

    def _index_tasks(self, db):
        documents = []
        for task in db.tasks():
            # Mail-linked custom tasks remain local; never index their mail body by implication.
            if task["provider"] == "custom" or not task.get("source_course_id"):
                continue
            material = task["provider"] == "brightspace" and task["external_id"].startswith(
                "content-"
            )
            facts = {
                key: task.get(key)
                for key in (
                    "due_at",
                    "available_at",
                    "closes_at",
                    "submission_status",
                    "source_status_known",
                    "source_availability",
                    "source_state",
                    "field_availability",
                    "last_seen_at",
                )
            }
            facts["due_at"] = task.get("source_due_at", task.get("due_at"))
            unavailable = [
                field
                for field in sorted(unread_fields(task.get("raw_data", {})))
                if field
                in {
                    "description",
                    "due_at",
                    "available_at",
                    "closes_at",
                    "submission_status",
                    "graded",
                    "score",
                    "points_possible",
                }
            ]
            complete = task.get("source_availability", "present") == "present" and not unavailable
            warnings = []
            if task.get("source_availability") in {"missing", "unconfirmed"}:
                warnings.append("Source data was not refreshed for " + task["title"])
            if unavailable:
                warnings.append(
                    "Unavailable fields for "
                    + task["title"]
                    + ": "
                    + ", ".join(field.replace("_", " ") for field in unavailable)
                )
            document = {
                "id": "task:" + task["id"],
                "provider": task["provider"],
                "course_id": task["source_course_id"],
                "title": task["title"],
                "body": "Source fields:\n"
                + json.dumps(facts, ensure_ascii=False)
                + "\nLocal overrides (not source facts):\n"
                + json.dumps(
                    {
                        key: task["local"].get(key)
                        for key in (
                            "due_override",
                            "due_at_override",
                            "completion_override",
                            "dismissed",
                        )
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
                + task.get("description", ""),
                "url": task.get("url"),
                "kind": "assignment",
                "updated_at": task.get("source_updated_at") or task["last_seen_at"],
                "source_task_id": task["id"],
                "fetched_at": task["last_seen_at"],
                "checked_at": task["last_seen_at"],
                "source_modified_at": task.get("source_updated_at"),
                "source_fields": facts,
                "complete": bool(complete),
                "warnings": warnings,
            }
            # Scheduled content is both actionable task evidence and a material page.
            # Keep source deadline/status updates independent of the extracted document body.
            documents.append(document)
            if material and self.get(task["id"]) is None:
                documents.append(
                    {key: value for key, value in document.items() if key != "source_fields"}
                    | {
                        "id": task["id"],
                        "kind": "material",
                        "body": task.get("description", ""),
                        "complete": False,
                        "fetched_at": None,
                        "checked_at": task["last_seen_at"],
                        "warnings": ["Legacy material has not yet been retrieved as a document"],
                    }
                )
        return self.upsert_documents(documents)

    def _search_rows(self, course_ids, provider, kind):
        # Keep one bounded scope, not a copy for every keystroke/query. Recompute
        # ranking and freshness on each request, retaining exact search semantics.
        key = (tuple(sorted(course_ids)) if course_ids is not None else None, provider, kind)
        with self._search_lock:
            before = self._revision.token()
            if self._search_cache and self._search_cache[:2] == (before, key):
                return self._search_cache[2]
            with self.connection() as db:
                conditions, args = [], []
                if course_ids is not None:
                    conditions.append("course_id IN (" + ",".join("?" for _ in course_ids) + ")")
                    args.extend(course_ids)
                for column, value in (("provider", provider), ("kind", kind)):
                    if value:
                        conditions.append(column + "=?")
                        args.append(value)
                sql = "SELECT * FROM documents" + (
                    " WHERE " + " AND ".join(conditions) if conditions else ""
                )
                rows = db.execute(sql, args).fetchall()
            prepared = [
                (row, normalized_text(row["title"]), normalized_text(row["body"])) for row in rows
            ]
            after = self._revision.token()
            if before == after and sum(len(row["body"]) for row in rows) <= 8_000_000:
                self._search_cache = (before, key, prepared)
            else:
                self._search_cache = None
            return prepared

    def search(self, query: str, course_ids: list[str] | None = None, limit=8, **filters):
        return self._ranked_documents(query, course_ids, **filters)[: max(1, min(limit, 20))]

    def search_page(self, query, course_ids, offset=0, limit=8):
        documents = [
            document
            for document in self._ranked_documents(query, course_ids)
            if document["kind"] not in {"mail", "email"} and document["provider"] != "gmail"
        ]
        return {
            "documents": documents[offset : offset + limit],
            "total": len(documents),
            "offset": offset,
            "has_more": offset + limit < len(documents),
        }

    def _ranked_documents(
        self, query, course_ids, *, provider=None, kind=None, freshness="all", completeness="all"
    ):
        if course_ids == []:
            return []
        if freshness not in {"all", "recent", "stale", "unknown"}:
            raise ValueError("Unknown freshness filter")
        if completeness not in {"all", "complete", "incomplete", "unknown"}:
            raise ValueError("Unknown completeness filter")
        terms = search_terms(query)
        phrases = [normalized_text(text).strip() for text in re.findall(r'["“]([^"”]+)["”]', query)]
        phrase = normalized_text(query).strip().strip('"“”')
        rows = self._search_rows(course_ids, provider, kind)
        ranked = []
        current = now()
        for row, title, body in rows:
            document = self._document(row)
            age = material_freshness(document, current)
            complete = document.get("complete")
            state = (
                "complete" if complete is True else "incomplete" if complete is False else "unknown"
            )
            if freshness != "all" and age != freshness:
                continue
            if completeness != "all" and state != completeness:
                continue
            if any(part not in title and part not in body for part in phrases):
                continue
            score, matched = 0.0, []
            for term, weight in terms.items():
                pattern = (
                    re.escape(term)
                    if re.search(r"[\u3400-\u9fff]", term)
                    else r"(?<!\w)" + re.escape(term) + r"(?!\w)"
                )
                title_count, body_count = (
                    len(re.findall(pattern, title)),
                    len(re.findall(pattern, body)),
                )
                if title_count or body_count:
                    matched.append(term)
                    score += weight * (20 * min(title_count, 2) + 2 * min(body_count, 5) + 5)
            if phrase and matched:
                score += 100 if title == phrase else 35 if phrase in title else 0
                score += 12 if phrase in body else 0
            if score or not terms:
                document.update(search_score=round(score, 2), matched_terms=matched, freshness=age)
                ranked.append(document)
        ranked.sort(key=lambda item: (-item["search_score"], item["id"]))
        if not terms:
            ranked.sort(key=lambda item: item.get("fetched_at") or "", reverse=True)
        return ranked

    def library(self, query="", course_ids=None, offset=0, limit=50, **filters):
        documents = [
            document
            for document in self._ranked_documents(query, course_ids, **filters)
            if document["kind"] != "assignment"
        ]
        page = []
        for document in documents[offset : offset + limit]:
            excerpt, start = search_excerpt(document["body"], query)
            page.append(
                document
                | {
                    "body": excerpt,
                    "body_start": start,
                    "body_truncated": len(document["body"]) > len(excerpt),
                }
            )
        return {"documents": page, "total": len(documents)}

    def config(self):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM configuration WHERE key='chat'").fetchone()
        return json.loads(row[0]) if row else {"base_url": "https://api.openai.com/v1", "model": ""}

    def save_config(self, value):
        # Accept only nonsecret configuration, even if a caller accidentally passes a key.
        safe = {key: value[key] for key in ("base_url", "model")}
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO configuration VALUES ('chat', ?)", (json.dumps(safe),)
            )

    def create_conversation(self, title: str, course_id: str | None, task_id: str | None = None):
        key, stamp = uuid4().hex, now().isoformat()
        with self.connection() as db:
            db.execute(
                "INSERT INTO conversations (id,title,course_id,created_at,updated_at,task_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, title[:100], course_id, stamp, stamp, task_id),
            )
        return key

    def conversations(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,course_id,updated_at,task_id FROM conversations "
                    "ORDER BY updated_at DESC"
                )
            ]

    def rename_conversation(self, key: str, title: str):
        with self.connection() as db:
            return (
                db.execute("UPDATE conversations SET title=? WHERE id=?", (title, key)).rowcount > 0
            )

    def delete_conversation(self, key: str):
        with self.connection() as db:
            db.execute("DELETE FROM messages WHERE conversation_id=?", (key,))
            return db.execute("DELETE FROM conversations WHERE id=?", (key,)).rowcount > 0

    def conversation(self, key: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (key,)).fetchone()
            if not row:
                return None
            messages = [
                dict(item)
                for item in db.execute(
                    "SELECT id,role,content,citations,created_at,warnings FROM messages "
                    "WHERE conversation_id=? ORDER BY rowid",
                    (key,),
                )
            ]
            activity = {
                item["message_id"]: json.loads(item["payload"])
                for item in db.execute(
                    "SELECT a.* FROM chat_activity a JOIN messages m ON m.id=a.message_id "
                    "WHERE m.conversation_id=?",
                    (key,),
                )
            }
        for item in messages:
            item["citations"] = json.loads(item["citations"])
            item["warnings"] = json.loads(item["warnings"])
            item["activity"] = activity.get(item["id"], [])
        return dict(row) | {"messages": messages}

    def add_message(
        self, conversation_id, role, content, citations=None, warnings=None, activity=None
    ):
        key, stamp = uuid4().hex, now().isoformat()
        citations = citations or []
        warnings = warnings or []
        with self.connection() as db:
            db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    conversation_id,
                    role,
                    content,
                    json.dumps(citations),
                    stamp,
                    json.dumps(warnings),
                ),
            )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))
            if activity:
                db.execute("INSERT INTO chat_activity VALUES (?, ?)", (key, json.dumps(activity)))
        return {
            "id": key,
            "role": role,
            "content": content,
            "citations": citations,
            "created_at": stamp,
            "warnings": warnings,
            "activity": activity or [],
        }

    def backup(self, destination: Path):
        with self.connection() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
