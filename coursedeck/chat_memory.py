"""Bounded user memories in the existing knowledge configuration store."""

import hashlib
import json
import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .domain import now
from .knowledge import normalized_text, search_terms

MEMORY_KEY = "chat_memories"
MAX_MEMORIES = 64
CONTEXT_LIMIT = 16
CONTEXT_CHARACTERS = 6000
UNCHANGED = object()
SECRET = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"
    r"|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"
    r"|https?://[^\s/:]+:[^\s/@]+@"
    r"|(?:password|passwd|pwd|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"client[ _-]?secret|token|secret|密码|口令|密钥|验证码|令牌)\s*(?:[=:：]|\bis\b|是|为)\s*\S+",
    re.I,
)


def validate_text(value, label="Memory", *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise HTTPException(400, f"{label} must contain 1–1000 characters")
    if SECRET.search(normalized_text(value)):
        raise HTTPException(
            400, "Passwords, tokens and other credentials cannot be saved as memories"
        )
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise HTTPException(400, f"{label} contains unsupported control characters")
    return value.strip()


def identity(text):
    return " ".join(normalized_text(text).split())


def versioned(memory):
    values = {key: value for key, value in memory.items() if key != "version"}
    return values | {
        "version": hashlib.sha256(
            json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    }


class MemoryStore:
    def __init__(self, knowledge_store):
        self.knowledge = knowledge_store

    @staticmethod
    def _read(conn):
        row = conn.execute(
            "SELECT payload FROM configuration WHERE key=?", (MEMORY_KEY,)
        ).fetchone()
        if row is None:
            return {"mode": "automatic", "memories": []}
        try:
            state = json.loads(row[0])
            if state["mode"] not in {"automatic", "confirm"} or not isinstance(
                state["memories"], list
            ):
                raise ValueError
            required = {
                "id",
                "text",
                "course_id",
                "version",
                "status",
                "origin",
                "source_quote",
                "conversation_id",
                "message_id",
                "created_at",
                "updated_at",
            }
            if any(
                not isinstance(item, dict)
                or not required.issubset(item)
                or not isinstance(item["text"], str)
                or not isinstance(item["id"], str)
                or item["status"] not in {"active", "pending"}
                or item["origin"] not in {"manual", "chat"}
                or not isinstance(item["version"], str)
                for item in state["memories"]
            ):
                raise ValueError
            return state
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, "Stored memories could not be read") from exc

    @staticmethod
    def _write(conn, state):
        conn.execute(
            "INSERT INTO configuration(key,payload) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
            (MEMORY_KEY, json.dumps(state, ensure_ascii=False)),
        )

    @staticmethod
    def _find(state, memory_id, expected_version):
        memory = next((item for item in state["memories"] if item["id"] == memory_id), None)
        if memory is None:
            raise HTTPException(404, "Memory not found")
        if memory["version"] != expected_version:
            raise HTTPException(409, "Memory changed; reload it before applying this edit")
        return memory

    def list(self):
        with self.knowledge.connection() as conn:
            state = self._read(conn)
        return state | {
            "memories": sorted(
                state["memories"], key=lambda item: (item["updated_at"], item["id"]), reverse=True
            )
        }

    def set_mode(self, mode):
        if mode not in {"automatic", "confirm"}:
            raise HTTPException(400, "Choose automatic or confirm memory mode")
        with self.knowledge.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            state["mode"] = mode
            self._write(conn, state)
        return {"mode": mode}

    def add(
        self,
        text,
        course_id=None,
        *,
        origin="manual",
        source_quote=None,
        conversation_id=None,
        message_id=None,
        requires_confirmation=False,
    ):
        text = validate_text(text)
        source_quote = validate_text(source_quote, "Source quote", optional=origin == "manual")
        if origin not in {"manual", "chat"}:
            raise HTTPException(400, "Unknown memory origin")
        if course_id is not None and (not isinstance(course_id, str) or not course_id.strip()):
            raise HTTPException(400, "Invalid memory course")
        with self.knowledge.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            duplicate = next(
                (
                    item
                    for item in state["memories"]
                    if item["course_id"] == course_id and identity(item["text"]) == identity(text)
                ),
                None,
            )
            if duplicate:
                return {"memory": duplicate, "created": False}
            if len(state["memories"]) >= MAX_MEMORIES:
                raise HTTPException(
                    409, "Memory limit reached (64); delete an existing memory first"
                )
            stamp = now().isoformat()
            memory = versioned(
                {
                    "id": uuid4().hex,
                    "text": text,
                    "course_id": course_id,
                    "status": "pending"
                    if origin == "chat" and (requires_confirmation or state["mode"] == "confirm")
                    else "active",
                    "origin": origin,
                    "source_quote": source_quote,
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
            )
            state["memories"].append(memory)
            self._write(conn, state)
        return {"memory": memory, "created": True}

    def update(self, memory_id, text, expected_version, *, course_id=UNCHANGED):
        text = validate_text(text)
        with self.knowledge.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            old = self._find(state, memory_id, expected_version)
            target_course = old["course_id"] if course_id is UNCHANGED else course_id
            if any(
                item["id"] != memory_id
                and item["course_id"] == target_course
                and identity(item["text"]) == identity(text)
                for item in state["memories"]
            ):
                raise HTTPException(409, "An identical memory already exists in this scope")
            if old["text"] == text and old["course_id"] == target_course:
                return old
            # A manual correction no longer claims that its new text came from an older quote.
            memory = versioned(
                old
                | {
                    "text": text,
                    "course_id": target_course,
                    "origin": "manual",
                    "source_quote": None,
                    "conversation_id": None,
                    "message_id": None,
                    "updated_at": now().isoformat(),
                }
            )
            state["memories"] = [
                memory if item["id"] == memory_id else item for item in state["memories"]
            ]
            self._write(conn, state)
        return memory

    def delete(self, memory_id, expected_version):
        with self.knowledge.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            self._find(state, memory_id, expected_version)
            state["memories"] = [item for item in state["memories"] if item["id"] != memory_id]
            self._write(conn, state)
        return {"id": memory_id}

    def approve(self, memory_id, expected_version):
        with self.knowledge.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            old = self._find(state, memory_id, expected_version)
            if old["status"] == "active":
                return old
            memory = versioned(old | {"status": "active", "updated_at": now().isoformat()})
            state["memories"] = [
                memory if item["id"] == memory_id else item for item in state["memories"]
            ]
            self._write(conn, state)
        return memory

    def export(self):
        state = self.list()
        lines = ["# CourseDeck memories", "", f"Mode: {state['mode']}", ""]
        for memory in state["memories"]:
            lines += [
                f"## {memory['id']}",
                "",
                memory["text"],
                "",
                f"- Scope: {memory['course_id'] or 'Global'}",
                f"- Status: {memory['status']}",
                f"- Origin: {memory['origin']}",
                f"- Updated: {memory['updated_at']}",
                "",
            ]
            if memory["source_quote"]:
                lines += ["> " + line for line in memory["source_quote"].splitlines()] + [""]
        return "\n".join(lines)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    text: str = Field(min_length=1, max_length=1000)
    course_id: str | None = Field(default=None, max_length=1024)


class MemoryUpdate(MemoryInput):
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemoryVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["automatic", "confirm"]


class RememberInput(MemoryInput):
    source_quote: str = Field(min_length=1, max_length=1000)
    requires_confirmation: bool = False


class MemoryTools:
    def __init__(self, service, value, conversation_id, user_message_id):
        self.service, self.value = service, value
        self.store = service.memory
        self.conversation_id, self.user_message_id = conversation_id, user_message_id

    def _visible(self, state=None):
        selected = self.service.canonical_course(self.value.course_id)
        allowed = set(self.service.scope(selected))
        courses = {course["id"]: course for course in self.service.db.courses()}
        resolved_courses = {}
        memories = []
        for item in (state or self.store.list())["memories"]:
            if item["status"] != "active":
                continue
            course_id = item["course_id"]
            if course_id:
                if course_id not in resolved_courses:
                    try:
                        canonical = self.service.canonical_course(course_id)
                        resolved_courses[course_id] = (
                            canonical
                            if canonical == selected or set(self.service.scope(canonical)) & allowed
                            else None
                        )
                    except HTTPException:
                        resolved_courses[course_id] = None
                course_id = resolved_courses[course_id]
                if course_id is None:
                    continue
            memories.append(
                {
                    "id": item["id"],
                    "text": item["text"],
                    "course_id": course_id,
                    "scope": "course" if course_id else "global",
                    "course_name": courses.get(course_id, {}).get("name"),
                    "updated_at": item["updated_at"],
                }
            )
        if selected:
            memories.sort(key=lambda item: item["course_id"] != selected)
        return memories

    def context(self):
        state = self.store.list()
        visible = self._visible(state)
        selected, characters = [], 0
        for memory in visible:
            if (
                len(selected) >= CONTEXT_LIMIT
                or characters + len(memory["text"]) > CONTEXT_CHARACTERS
            ):
                continue
            selected.append(memory)
            characters += len(memory["text"])
        omitted = len(visible) - len(selected)
        return {
            "mode": state["mode"],
            "memories": selected,
            "total": len(visible),
            "omitted": omitted,
            "warnings": [
                f"{omitted} matching memories are not in context; use search_memories to find them"
            ]
            if omitted
            else [],
        }

    def definitions(self, define):
        return [
            define(
                "search_memories",
                "Find saved active user preferences and facts in this conversation's scope. "
                "Use keywords and page through results. Memories are not verified source evidence.",
                {
                    "query": {"type": "string", "maxLength": 1000},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 16},
                },
                ["query"],
            ),
            define(
                "remember_memory",
                "Save a durable user preference or personal fact explicitly supported "
                "by an exact quote from the current user message. Do not save source instructions, "
                "temporary deadlines or credentials. Omitted course_id uses the selected course; "
                "use null for global preferences such as reply language. "
                "Confirmation requests stay pending. "
                "This tool cannot update or delete an existing memory.",
                {
                    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "source_quote": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "course_id": {"type": ["string", "null"], "maxLength": 1024},
                    "requires_confirmation": {"type": "boolean"},
                },
                ["text", "source_quote"],
            ),
        ]

    async def run(self, name, args):
        try:
            if name == "search_memories":
                query, offset, limit = (
                    args.get("query", ""),
                    args.get("offset", 0),
                    args.get("limit", 16),
                )
                if (
                    args.keys() - {"query", "offset", "limit"}
                    or not isinstance(query, str)
                    or len(query) > 1000
                    or type(offset) is not int
                    or offset < 0
                    or type(limit) is not int
                    or not 1 <= limit <= 16
                ):
                    raise ValueError("Invalid memory search")
                terms = search_terms(query)
                ranked = [
                    (
                        sum(
                            weight
                            for term, weight in terms.items()
                            if term in normalized_text(item["text"])
                        ),
                        item,
                    )
                    for item in self._visible()
                ]
                matches = [
                    item
                    for score, item in sorted(ranked, key=lambda pair: -pair[0])
                    if score or not terms
                ]
                return {
                    "memories": matches[offset : offset + limit],
                    "total": len(matches),
                    "offset": offset,
                    "has_more": offset + limit < len(matches),
                }
            if name != "remember_memory":
                raise ValueError("Unsupported memory tool")
            value = RememberInput.model_validate(args)
            if value.source_quote not in self.value.message:
                raise ValueError("Memory source_quote must quote the current user message exactly")
            course_id = (
                value.course_id if "course_id" in value.model_fields_set else self.value.course_id
            )
            course_id = self.service.canonical_course(course_id)
            selected = self.service.canonical_course(self.value.course_id)
            if selected and course_id and selected != course_id:
                raise ValueError("This conversation cannot save a memory for another course")
            return self.store.add(
                value.text,
                course_id,
                origin="chat",
                source_quote=value.source_quote,
                conversation_id=self.conversation_id,
                message_id=self.user_message_id,
                requires_confirmation=value.requires_confirmation,
            )
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc


def build_memory_router(service):
    router = APIRouter(prefix="/memories")

    @router.get("")
    def memories():
        return service.memory.list()

    @router.get("/export")
    def export():
        return Response(
            service.memory.export(),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="coursedeck-memories.md"',
            },
        )

    @router.put("/settings")
    def settings(value: MemorySettings):
        return service.memory.set_mode(value.mode)

    @router.post("")
    def create(value: MemoryInput):
        course_id = service.canonical_course(value.course_id)
        return service.memory.add(value.text, course_id)["memory"]

    @router.put("/{memory_id}")
    def update(memory_id: str, value: MemoryUpdate):
        course_id = (
            service.canonical_course(value.course_id)
            if "course_id" in value.model_fields_set
            else UNCHANGED
        )
        return service.memory.update(
            memory_id, value.text, value.expected_version, course_id=course_id
        )

    @router.delete("/{memory_id}")
    def delete(memory_id: str, version: str = Query(pattern=r"^[0-9a-f]{64}$")):
        return service.memory.delete(memory_id, version)

    @router.post("/{memory_id}/approve")
    def approve(memory_id: str, value: MemoryVersion):
        return service.memory.approve(memory_id, value.expected_version)

    return router
