"""Scoped task tools shared by the chat runner; no model access to SQL or arbitrary URLs."""

import re

from .task_edits import TaskEdit, TaskEdits
from .task_links import TaskLinkReader, task_links
from .task_permissions import normalized, parse_intent, resolve_target


def edit_requested(message):
    return parse_intent(message) is not None


class TaskTools:
    def __init__(self, service, scope, value, expose):
        self.service, self.scope, self.value, self.expose = service, scope, value, expose
        self.edits = service.task_edits
        self.reader = service.link_reader
        self.read_versions, self.documents = {}, {}
        self.intent = parse_intent(value.message, service.db.settings().timezone)
        self.target_id = None
        self.undo_id = None
        if self.intent:
            candidates = [task for task in service.db.tasks() if self.allowed(task)]
            self.target_id = resolve_target(self.intent, candidates, value.task_id)
            if self.intent.undo:
                if normalized(self.intent.target) in {
                    "that change",
                    "last change",
                    "刚才的修改",
                    "上次修改",
                }:
                    conversation = (
                        service.store.conversation(value.conversation_id)
                        if value.conversation_id
                        else None
                    )
                    for message in reversed((conversation or {}).get("messages", [])):
                        receipts = [item.get("change", {}) for item in message.get("activity", [])]
                        found = next(
                            (item for item in reversed(receipts) if item.get("change_id")), None
                        )
                        if found:
                            if found.get("task_id") in {task["id"] for task in candidates}:
                                self.target_id, self.undo_id = found["task_id"], found["change_id"]
                            break
                elif self.target_id:
                    self.undo_id = next(
                        (
                            item["change_id"]
                            for item in self.edits.recent(self.target_id)
                            if not item["undone"]
                        ),
                        None,
                    )

    def validate_edit(self, name, args):
        if not self.intent or not self.target_id:
            raise ValueError("Ask the user for an explicit, unambiguous local edit instruction")
        if args.get("task_id") != self.target_id:
            raise ValueError("This instruction does not authorize editing that task")
        if name == "undo_change":
            if not self.intent.undo or not self.undo_id or args.get("change_id") != self.undo_id:
                raise ValueError("This instruction does not authorize undoing that change")
            if set(args) != {"task_id", "expected_version", "change_id"}:
                raise ValueError("Unsupported undo fields")
            return
        if self.intent.undo:
            raise ValueError("This instruction only authorizes undo")
        supplied = {
            key: value for key, value in args.items() if key not in {"task_id", "expected_version"}
        }
        wanted = self.intent.fields
        # Compare parsed instants, not spelling of equivalent timezone offsets.
        common = {"task_id": self.target_id, "expected_version": args.get("expected_version")}
        actual = TaskEdit.model_validate(common | supplied).model_dump(
            mode="json", exclude_unset=True
        )
        expected = TaskEdit.model_validate(common | wanted).model_dump(
            mode="json", exclude_unset=True
        )
        if "due_at" in supplied and supplied.get("due_at") and wanted.get("due_at"):
            from datetime import datetime

            actual["due_at"] = datetime.fromisoformat(
                actual["due_at"].replace("Z", "+00:00")
            ).timestamp()
            expected["due_at"] = datetime.fromisoformat(
                expected["due_at"].replace("Z", "+00:00")
            ).timestamp()
        if actual != expected:
            raise ValueError("This instruction does not authorize those fields or values")

    def guard_edit(self, name, args):
        """Run under the task edit write transaction, before changing local state."""
        self.get(args.get("task_id"))
        self.validate_edit(name, args)
        if not self.intent.undo:
            candidates = [task for task in self.service.db.tasks() if self.allowed(task)]
            if resolve_target(self.intent, candidates, self.value.task_id) != self.target_id:
                raise ValueError("Task matching changed; give an unambiguous edit instruction")

    def allowed(self, task):
        active_courses = [
            course
            for course in self.service.db.courses()
            if not course.get("disabled") and not course.get("deleted")
        ]
        if task.get("course_id") and not any(
            course["id"] == task["course_id"] for course in active_courses
        ):
            return False
        if self.value.task_id and task["id"] == self.value.task_id:
            return True
        return (
            task.get("source_course_id") in self.scope
            or (
                task.get("provider") == "custom"
                and not task.get("course_id")
                and not self.value.course_id
                and not self.value.task_id
            )
            or bool(
                task.get("course_id")
                and any(
                    course["id"] == task["course_id"]
                    and set(course["source_course_ids"]) & set(self.scope)
                    for course in active_courses
                )
            )
        )

    def get(self, task_id):
        task = next((task for task in self.service.db.tasks() if task["id"] == task_id), None)
        if not task or not self.allowed(task):
            raise ValueError("Task is outside this conversation or no longer available")
        return task

    def summary(self, task):
        return {
            key: task.get(key)
            for key in (
                "id",
                "title",
                "course_id",
                "source_course_id",
                "provider",
                "due_at",
                "source_due_at",
                "submission_status",
                "source_status_known",
                "source_availability",
                "last_seen_at",
            )
        } | {"local": task["local"]}

    def links(self, task):
        context = self.service.task_context(task["id"])
        return task_links(task, context["documents"])

    async def run(self, name, args):
        if name == "find_tasks":
            query, offset, limit = (
                args.get("query", ""),
                args.get("offset", 0),
                args.get("limit", 20),
            )
            if (
                not isinstance(query, str)
                or len(query) > 2000
                or type(offset) is not int
                or offset < 0
                or type(limit) is not int
                or not 1 <= limit <= 50
            ):
                raise ValueError("Invalid search parameters")
            terms = re.findall(r"\w+", query.casefold())
            courses = {course["id"]: course["name"] for course in self.service.db.courses()}
            matches = []
            for task in self.service.db.tasks():
                if not self.allowed(task):
                    continue
                haystack = " ".join(
                    str(task.get(key) or "")
                    for key in ("title", "description", "provider", "course_id")
                )
                haystack += " " + courses.get(task.get("course_id"), "")
                if all(term in haystack.casefold() for term in terms):
                    matches.append(self.summary(task))
            return {
                "tasks": matches[offset : offset + limit],
                "total": len(matches),
                "offset": offset,
            }
        task = self.get(args.get("task_id"))
        if name == "get_task":
            # A source sync or UI edit must not attach its newer version to older
            # facts. Retry one concurrent change, then ask for a fresh read.
            for _ in range(2):
                version = self.edits.version(task["id"])
                task = self.get(task["id"])
                if version == self.edits.version(task["id"]):
                    break
            else:
                raise ValueError("Task changed while reading; read it again")
            self.read_versions[task["id"]] = version
            document = self.service.store.get("task:" + task["id"])
            return {
                "task": self.summary(task)
                | {
                    "description": (task.get("description") or "")[:12000],
                    "description_truncated": len(task.get("description") or "") > 12000,
                    "version": version,
                },
                "links": self.links(task),
                "documents": self.expose([document] if document else []),
                "recent_changes": self.edits.recent(task["id"]),
            }
        if name == "read_task_link":
            link = next(
                (link for link in self.links(task) if link["link_id"] == args.get("link_id")), None
            )
            if not link:
                raise ValueError(
                    "Read a link ID returned by get_task; arbitrary URLs are not accepted"
                )
            offset, limit = args.get("offset", 0), args.get("limit", 6500)
            if (
                type(offset) is not int
                or offset < 0
                or type(limit) is not int
                or not 1 <= limit <= 12000
            ):
                raise ValueError("Invalid excerpt range")
            if link["link_id"] not in self.documents:
                if len(self.documents) >= 4:
                    raise ValueError("Four links have been read; continue in another message")
                self.documents[link["link_id"]] = await self.reader.read(task, link)
            document = self.documents[link["link_id"]]
            return {
                "documents": self.expose(
                    [document], offset=offset, limit=limit, task_id=task["id"]
                ),
                "related_via": link["via"],
            }
        if name in {"update_task", "undo_change"}:
            self.validate_edit(name, args)
            if self.read_versions.get(task["id"]) != args.get("expected_version"):
                raise ValueError("Use get_task in this turn before editing")
            if name == "update_task":
                result = self.edits.update(
                    TaskEdit.model_validate(args), guard=lambda: self.guard_edit(name, args)
                )
            else:
                result = self.edits.undo(
                    task["id"],
                    args["change_id"],
                    args["expected_version"],
                    guard=lambda: self.guard_edit(name, args),
                )
            self.service.store.index_tasks(self.service.db)
            self.read_versions.pop(task["id"], None)
            return result | {"scope": "CourseDeck local override only"}
        raise ValueError("Unsupported task tool")

    def definitions(self, define):
        task_id = {"type": "string", "maxLength": 2048}
        version = {"type": "string", "minLength": 64, "maxLength": 64}
        paging = {
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
        }
        result = [
            define(
                "find_tasks",
                "Find tasks in this conversation's course scope. Use short title/course keywords; "
                "empty query lists tasks. Page through results; clarify ambiguous matches.",
                {
                    "query": {"type": "string", "maxLength": 2000},
                    **paging,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["query"],
            ),
            define(
                "get_task",
                "Read current source facts, local overrides, version, related link IDs "
                "and recent reversible changes.",
                {"task_id": task_id},
                ["task_id"],
            ),
            define(
                "read_task_link",
                "Read one related link on demand, using a link_id from get_task. "
                "Does not bulk download or store the full document. "
                "Keyword matches are not confirmed associations.",
                {"task_id": task_id, "link_id": {"type": "string"}, **paging},
                ["task_id", "link_id"],
            ),
        ]
        if self.intent and self.target_id and (not self.intent.undo or self.undo_id):
            result += [
                define(
                    "update_task",
                    "Apply ONLY the local edit explicitly requested by the user after get_task. "
                    "Never change source facts. Include due_at only when changing it; "
                    "null explicitly clears the deadline. Dates require timezone offsets. "
                    "Ask if task/date is ambiguous.",
                    {
                        "task_id": task_id,
                        "expected_version": version,
                        "due_at": {"type": ["string", "null"], "format": "date-time"},
                        "restore_due_date": {"type": "boolean"},
                        "completion": {"type": "string", "enum": ["done", "open", "source"]},
                        "note": {"type": "string", "maxLength": 10000},
                    },
                    ["task_id", "expected_version"],
                ),
                define(
                    "undo_change",
                    "Undo a recent local edit from get_task, after reading its current version. "
                    "Do not invent change IDs.",
                    {
                        "task_id": task_id,
                        "change_id": {"type": "string"},
                        "expected_version": version,
                    },
                    ["task_id", "change_id", "expected_version"],
                ),
            ]
        if self.intent and self.target_id:
            permitted = "undo_change" if self.intent.undo else "update_task"
            result = [
                item
                for item in result
                if item["function"]["name"] not in {"update_task", "undo_change"}
                or item["function"]["name"] == permitted
            ]
            for item in result:
                if item["function"]["name"] != permitted:
                    continue
                schema = item["function"]["parameters"]
                schema["properties"]["task_id"] = {
                    **schema["properties"]["task_id"],
                    "enum": [self.target_id],
                }
                if self.intent.undo:
                    schema["properties"]["change_id"]["enum"] = [self.undo_id]
                else:
                    allowed_fields = {"task_id", "expected_version", *self.intent.fields}
                    schema["properties"] = {
                        key: prop
                        for key, prop in schema["properties"].items()
                        if key in allowed_fields
                    }
                    schema["required"] = sorted(allowed_fields)
                    for key, value in self.intent.fields.items():
                        schema["properties"][key]["enum"] = [value]
        return result


def configure_task_tools(service):
    service.task_edits = TaskEdits(service.db)
    service.link_reader = TaskLinkReader(service.engine)
