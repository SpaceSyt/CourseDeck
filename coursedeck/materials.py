"""Course-scoped collection into the local knowledge store, separate from Todo."""

import asyncio
import re
from contextlib import asynccontextmanager

from playwright.async_api import Error as BrowserError

from .checkpoints import MaterialCheckpoints
from .connectors.brightspace_content import MAX_FILE_BYTES, enrich_content, rich_text
from .connectors.capabilities import connector_capabilities
from .connectors.http import TransportError
from .connectors.material_retry import retry_after_seconds, retry_material_read
from .domain import Course, Outcome, SyncResult, Task, now
from .knowledge import KnowledgeStore, safe_source_url

MAX_BODY_READS = 24
MAX_TOC_ITEMS = 1000
MATERIAL_RETRY_DELAYS = (0.5, 2)


def material_error(result, category, scope, stage, attempts=0):
    result.setdefault("errors", []).append(
        {"category": str(category), "scope": scope, "stage": stage, "retry_attempt": attempts}
    )


def source_courses(db, course_id):
    if hasattr(db, "resolve_course_alias"):
        course_id = db.resolve_course_alias(course_id) or course_id
    selected = next(
        (
            course
            for course in db.courses()
            if course_id in (course["id"], course.get("workspace_id"))
        ),
        None,
    )
    if selected:
        ids = set(selected.get("source_course_ids", [selected["id"]]))
    else:
        ids = {course_id}
    return [Course.model_validate(course) for course in db.source_courses() if course["id"] in ids]


def toc_entries(body):
    if not isinstance(body, dict) or not isinstance(body.get("Modules"), list):
        raise ValueError("Content index was not recognized")
    entries, seen = [], set()

    def walk(module):
        if not isinstance(module, dict) or "ModuleId" not in module:
            raise ValueError("Content module was not recognized")
        key = ("module", str(module["ModuleId"]))
        if key in seen:
            raise ValueError("Content index repeated an identity")
        seen.add(key)
        entries.append(("module", module))
        if not isinstance(module.get("Topics"), list) or not isinstance(
            module.get("Modules"), list
        ):
            raise ValueError("Content child lists were not recognized")
        for topic in module["Topics"]:
            if not isinstance(topic, dict) or "TopicId" not in topic:
                raise ValueError("Content topic was not recognized")
            key = ("topic", str(topic["TopicId"]))
            if key in seen:
                raise ValueError("Content index repeated an identity")
            seen.add(key)
            entries.append(("topic", topic))
        if len(entries) > MAX_TOC_ITEMS:
            raise ValueError("Content index exceeds its reading limit")
        for child in module["Modules"]:
            walk(child)

    for module in body["Modules"]:
        walk(module)
    return entries


def relevance(title, query):
    words = set(re.findall(r"[a-z0-9]{3,}", query.casefold()))
    if any(term in query for term in ("评分", "大纲", "要求", "政策")):
        words.update({"syllabus", "grading", "policy", "information"})
    if any(term in query for term in ("作业", "题目", "提交")):
        words.update({"homework", "assignment", "readme"})
    return sum(word in title.casefold() for word in words)


class MaterialCollector:
    def __init__(self, db, engine, data_dir, *, knowledge_store=None):
        self.db, self.engine, self.data_dir = db, engine, data_dir
        self.cache = {}
        self.knowledge = knowledge_store or KnowledgeStore(data_dir / "knowledge.sqlite3")
        self.checkpoints = MaterialCheckpoints(self.knowledge)

    def reset_runtime_cache(self):
        # Checkpoints are read fresh for each run, including after a database restore.
        self.cache.clear()

    @asynccontextmanager
    async def lock(self, provider, already_locked=False):
        if getattr(self.engine, "paused", False):
            raise RuntimeError("Material reading is paused for maintenance")
        if already_locked:
            yield
        else:
            async with self.engine.queue_lock, self.engine.locks[provider]:
                if getattr(self.engine, "paused", False):
                    raise RuntimeError("Material reading is paused for maintenance")
                yield

    async def fetch(self, course_id, query):
        courses = source_courses(self.db, course_id)
        if not courses:
            return {"documents": [], "warnings": ["No connected source course was found."]}
        result = {"documents": [], "warnings": []}
        for provider in dict.fromkeys(course.provider for course in courses):
            await self.collect(
                provider,
                [course for course in courses if course.provider == provider],
                query,
                result,
            )
        return result

    async def refresh_source(self, provider, course_ids=None, already_locked=False, courses=None):
        courses = (
            courses
            if courses is not None
            else [
                Course.model_validate(course)
                for course in self.db.source_courses()
                if course["provider"] == provider
                and (
                    course_ids is None
                    or course["id"] in course_ids
                    or course["external_id"] in course_ids
                )
            ]
        )
        result = {"documents": [], "warnings": []}
        try:
            await asyncio.wait_for(
                self.collect(provider, courses, "", result, already_locked), timeout=100
            )
        except TimeoutError:
            material_error(result, "network_error", provider, "materials_read")
            result["warnings"].append(
                "Material reading timed out; completed documents and cached content were retained."
            )
        return result

    def retain_document(self, document, result):
        previous = self.knowledge.get(document["id"])
        if (
            document.get("complete") is False
            and document.get("evidence") != "announcement_api"
            and previous
            and previous.get("body")
            and document.get("body") != previous["body"]
        ):
            document = document | {
                "body": previous["body"],
                "fetched_at": previous.get("fetched_at"),
                "warnings": list(
                    dict.fromkeys(
                        document.get("warnings", [])
                        + ["Latest material is incomplete; previous body retained."]
                    )
                ),
            }
        document = document | {
            "body": re.sub(
                r'https?://[^\s<>"\']+',
                lambda match: safe_source_url(match[0]) or "[link unavailable]",
                document.get("body", ""),
            ),
            "url": safe_source_url(document.get("url")),
        }
        self.knowledge.upsert_documents([document])
        existing = next(
            (index for index, row in enumerate(result["documents"]) if row["id"] == document["id"]),
            None,
        )
        if existing is None:
            result["documents"].append(document)
        else:
            result["documents"][existing] = document

    async def collect(self, provider, courses, query, result, already_locked=False):
        connector = self.engine.connectors.get(provider)
        if not connector or not courses:
            return
        strategy = connector_capabilities(connector, provider).materials
        if strategy is None:
            result["warnings"].append(
                f"{connector.display_name}: material discovery is not supported yet."
            )
            return
        if strategy not in {"brightspace_content", "classroom_pages"}:
            material_error(result, "unsupported_strategy", provider, "materials_read")
            result["warnings"].append(
                f"{connector.display_name}: configured material reader is not supported."
            )
            return
        connector = getattr(connector, "active", connector)
        async with self.lock(provider, already_locked):
            if connector.connection_status() != "connected":
                material_error(result, "auth_required", provider, "materials_read")
                result["warnings"].append(
                    f"{connector.display_name}: connect the source to read materials."
                )
                return
            browser = getattr(connector, "browser", None)
            if browser is None or getattr(connector, "config", {}).get("transport") == "api":
                result["warnings"].append(
                    f"{connector.display_name}: material reading requires its browser connection."
                )
                return
            try:
                async with browser.session(connector.config.get("timezone")) as context:
                    if not query:
                        courses = self.checkpoints.rotate(
                            provider, "courses", courses, lambda course: course.id
                        )
                    if strategy == "brightspace_content":
                        await connector.restore_browser_session(context)
                        for index, course in enumerate(courses):
                            if not query:
                                self.checkpoints.put(
                                    provider, "courses", courses[(index + 1) % len(courses)].id
                                )
                            await self.brightspace(context, connector, course, query, result)
                    else:
                        from .connectors.classroom_materials import collect_classroom_materials

                        page = await context.new_page()
                        for index, course in enumerate(courses):
                            if not query:
                                self.checkpoints.put(
                                    provider, "courses", courses[(index + 1) % len(courses)].id
                                )
                            collected = await collect_classroom_materials(
                                connector,
                                page,
                                course,
                                on_document=lambda document: self.retain_document(document, result),
                                resume_identity=None
                                if query
                                else self.checkpoints.get(provider, course.id),
                                on_checkpoint=None
                                if query
                                else lambda identity, course=course: self.checkpoints.put(
                                    provider, course.id, identity
                                ),
                                max_body_reads=MAX_BODY_READS,
                            )
                            result["warnings"].extend(collected["warnings"])
                            if collected.get("errors"):
                                result.setdefault("errors", []).extend(collected["errors"])
            except TransportError as exc:
                material_error(result, exc.outcome, provider, "materials_read")
                result["warnings"].append(f"{connector.display_name}: {exc.safe_message}")
            except (BrowserError, ValueError, TypeError, KeyError):
                material_error(result, "read_error", provider, "materials_read")
                result["warnings"].append(
                    f"{connector.display_name}: materials could not be fully read; "
                    "cached content retained."
                )

    async def brightspace(self, context, connector, course, query, output):
        base, version = connector.base_url, connector.config.get("le_version", "1.82")
        known_task_ids = {task["id"] for task in self.db.tasks()}

        async def request(path, **kwargs):
            async def read():
                try:
                    response = await context.request.get(base + path, timeout=30000, **kwargs)
                    if response.status != 200:
                        category = (
                            Outcome.RATE_LIMITED
                            if response.status == 429
                            else Outcome.NETWORK_ERROR
                            if response.status >= 500
                            else Outcome.AUTH_REQUIRED
                            if response.status == 401
                            else Outcome.PARTIAL
                        )
                        error = TransportError(
                            category, f"Material resource unavailable (HTTP {response.status})."
                        )
                        retry_after = getattr(response, "headers", {}).get("retry-after", "")
                        error.retry_after = retry_after_seconds(retry_after)
                        raise error
                    return response
                except BrowserError as exc:
                    raise TransportError(Outcome.NETWORK_ERROR, "Material request failed.") from exc

            return await retry_material_read(
                read,
                errors=output.setdefault("errors", []),
                scope=course.id,
                stage="material_resource",
                delays=MATERIAL_RETRY_DELAYS,
            )

        async def get(path, params=None):
            try:
                response = await request(path, params=params)
                return await response.json()
            except BrowserError as exc:
                raise TransportError(Outcome.NETWORK_ERROR, "Material request failed.") from exc
            except ValueError as exc:
                raise TransportError(
                    Outcome.PARSE_ERROR, "Material response was not recognized."
                ) from exc

        async def download(path):
            response = await request(path, max_redirects=0)
            body = await response.body()
            if len(body) > MAX_FILE_BYTES:
                raise TransportError(Outcome.PARTIAL, "Material file exceeds its reading limit.")
            return body, response.headers.get("content-type", "")

        try:
            body = await get(f"/d2l/api/le/{version}/{course.external_id}/content/toc")
            entries = toc_entries(body)
            entries.sort(key=lambda entry: -relevance(entry[1].get("Title", ""), query))

            def identity(entry):
                kind, raw = entry
                return f"{kind}:{raw['ModuleId' if kind == 'module' else 'TopicId']}"

            if not query:
                entries = self.checkpoints.rotate("brightspace", course.id, entries, identity)
            reads, deferred, restricted = 0, 0, 0
            for kind, raw in entries:
                # Save the current unread identity before any cancellable request.
                # Once the budget is exhausted, leave the first deferred item pinned.
                if not query and not deferred:
                    self.checkpoints.put("brightspace", course.id, identity((kind, raw)))
                if raw.get("IsHidden") is True or raw.get("IsLocked") is True:
                    restricted += 1
                    continue
                item_id = str(raw["ModuleId" if kind == "module" else "TopicId"])
                title = raw.get("Title")
                if not item_id.isdecimal() or not isinstance(title, str) or not title:
                    output["warnings"].append(
                        f"{course.name}: a material identity was not recognized."
                    )
                    continue
                doc_id = f"brightspace:{course.external_id}:content-{item_id}"
                source_url = (
                    f"{base}/d2l/le/content/{course.external_id}/Home?itemIdentifier="
                    "D2L.LE.Content.ContentObject."
                    f"{'ModuleCO' if kind == 'module' else 'TopicCO'}-{item_id}"
                )
                stamp = raw.get("LastModifiedDate")
                cached = self.cache.get(doc_id)
                if cached is None:
                    saved = self.knowledge.get(doc_id)
                    if saved and saved.get("source_modified_at") and saved.get("complete") is True:
                        cached = (saved["source_modified_at"], saved)
                        self.cache[doc_id] = cached
                if stamp and cached and cached[0] == stamp and cached[1].get("complete") is True:
                    self.retain_document(cached[1] | {"checked_at": now().isoformat()}, output)
                    output["warnings"].extend(cached[1].get("warnings", []))
                    continue
                indexed = {
                    "id": doc_id,
                    "provider": "brightspace",
                    "course_id": course.id,
                    "title": title,
                    "body": "",
                    "url": source_url,
                    "kind": "material",
                    "updated_at": stamp or now().isoformat(),
                    "source_task_id": doc_id if doc_id in known_task_ids else None,
                    "source_modified_at": stamp,
                    "complete": False,
                    "evidence": "content_index",
                    "checked_at": now().isoformat(),
                    "warnings": ["Material body has not been read."],
                }
                self.retain_document(indexed, output)
                if reads >= MAX_BODY_READS:
                    deferred += 1
                    continue
                reads += 1
                metadata_path = (
                    f"/d2l/api/le/{version}/{course.external_id}/content/"
                    f"{'modules' if kind == 'module' else 'topics'}/{item_id}"
                )
                try:
                    detail = await get(metadata_path)
                except TransportError as exc:
                    self.retain_document(indexed | {"warnings": [exc.safe_message]}, output)
                    output["warnings"].append(f"{course.name}: {title} — {exc.safe_message}")
                    continue
                stamp = detail.get("LastModifiedDate") if isinstance(detail, dict) else None
                if stamp and cached and cached[0] == stamp and cached[1].get("complete") is True:
                    self.retain_document(cached[1] | {"checked_at": now().isoformat()}, output)
                    output["warnings"].extend(cached[1].get("warnings", []))
                    continue

                async def content_get(path, params, detail=detail, metadata_path=metadata_path):
                    return detail if path == metadata_path else await get(path, params)

                task = Task(
                    provider="brightspace",
                    course_external_id=course.external_id,
                    external_id=f"content-{item_id}",
                    title=title,
                    url=source_url,
                )
                result = SyncResult(outcome=Outcome.PARTIAL)
                await enrich_content(content_get, download, base, version, course, task, result)
                output["warnings"].extend(result.warnings)
                if "description" in task.raw_data.get("unavailable_fields", []):
                    self.retain_document(indexed | {"warnings": result.warnings}, output)
                    continue
                document = {
                    "id": doc_id,
                    "provider": "brightspace",
                    "course_id": course.id,
                    "title": title,
                    "body": task.description,
                    "url": task.url,
                    "kind": "material",
                    "updated_at": stamp or now().isoformat(),
                    "source_task_id": doc_id if doc_id in known_task_ids else None,
                    "source_modified_at": stamp,
                    "complete": not task.raw_data.get("linked_content_unread", False),
                    "evidence": task.raw_data.get("description_evidence"),
                    "warnings": result.warnings,
                    "checked_at": now().isoformat(),
                    "fetched_at": now().isoformat(),
                }
                self.retain_document(document, output)
                if stamp and document["complete"]:
                    self.cache[doc_id] = (stamp, document)
            if restricted:
                output["warnings"].append(
                    f"{course.name}: {restricted} hidden or locked material entries were not read."
                )
            if deferred:
                output["warnings"].append(
                    f"{course.name}: {deferred} material bodies await reading; "
                    "narrow the Chat request to prioritize them."
                )
            elif entries and not query:
                self.checkpoints.put("brightspace", course.id, identity(entries[0]))
        except (TransportError, ValueError, TypeError, KeyError) as exc:
            if not isinstance(exc, TransportError):
                material_error(output, "parse_error", course.id, "material_index")
            message = (
                exc.safe_message
                if isinstance(exc, TransportError)
                else "Content index could not be fully read."
            )
            output["warnings"].append(f"{course.name}: {message}")
        try:
            rows = await get(f"/d2l/api/le/{version}/{course.external_id}/news/")
            if not isinstance(rows, list):
                raise ValueError("Announcement list was not recognized")
            for raw in rows:
                if raw.get("IsHidden") is True or raw.get("IsDraft") is True:
                    continue
                news_id = str(raw["Id"])
                if not news_id.isdecimal():
                    raise ValueError("Announcement identity was not recognized")
                warnings = (
                    ["Announcement attachments have not been read."]
                    if raw.get("Attachments")
                    else []
                )
                output["warnings"].extend(
                    f"{course.name}: {raw['Title']} — {warning}" for warning in warnings
                )
                self.retain_document(
                    {
                        "id": f"brightspace:{course.external_id}:news-{news_id}",
                        "provider": "brightspace",
                        "course_id": course.id,
                        "title": raw["Title"],
                        "body": rich_text(raw["Body"]),
                        "url": f"{base}/d2l/le/news/{course.external_id}",
                        "kind": "announcement",
                        "complete": not warnings,
                        "warnings": warnings,
                        "evidence": "announcement_api",
                        "checked_at": now().isoformat(),
                        "fetched_at": now().isoformat(),
                        "updated_at": raw.get("LastModifiedDate")
                        or raw.get("StartDate")
                        or now().isoformat(),
                    },
                    output,
                )
        except (TransportError, ValueError, TypeError, KeyError) as exc:
            if not isinstance(exc, TransportError):
                material_error(output, "parse_error", course.id, "announcement_index")
            output["warnings"].append(
                f"{course.name}: announcements could not be fully read; cached content retained."
            )
