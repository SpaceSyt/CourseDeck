import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, now
from coursedeck.materials import MaterialCollector, source_courses, toc_entries


def setup(tmp_path, get):
    db = Database(tmp_path / "main.sqlite3")
    course = Course(
        provider="brightspace",
        external_id="1",
        name="Course",
        source_url="https://school.example/d2l/home/1",
    )

    @asynccontextmanager
    async def session(timezone):
        assert engine.queue_lock.locked() and engine.locks["brightspace"].locked()
        yield SimpleNamespace(request=SimpleNamespace(get=get))

    async def restore(context):
        pass

    connector = SimpleNamespace(
        key="brightspace",
        display_name="Brightspace",
        base_url="https://school.example",
        config={},
        browser=SimpleNamespace(session=session),
        restore_browser_session=restore,
        connection_status=lambda: "connected",
    )
    engine = SimpleNamespace(
        connectors={"brightspace": connector},
        queue_lock=asyncio.Lock(),
        locks={"brightspace": asyncio.Lock()},
    )
    return db, course, engine, MaterialCollector(db, engine, tmp_path)


class Response:
    def __init__(self, body, status=200):
        self.payload, self.status = body, status

    async def json(self):
        return self.payload


def index():
    return {
        "Modules": [
            {
                "ModuleId": 10,
                "Title": "Overview",
                "IsHidden": False,
                "IsLocked": False,
                "Modules": [],
                "Topics": [
                    {"TopicId": i, "Title": f"Notes {i}", "IsHidden": False, "IsLocked": False}
                    for i in (11, 12, 13)
                ],
            }
        ]
    }


def detail(item_id):
    return {
        "Id": item_id,
        "Type": 0 if item_id == 10 else 1,
        "Title": "Overview" if item_id == 10 else f"Notes {item_id}",
        "IsHidden": False,
        "IsLocked": False,
        "Description": {"Text": f"Useful body {item_id}"},
        "Url": "",
        "LastModifiedDate": "2026-09-10T01:00:00Z",
    }


@pytest.mark.asyncio
async def test_material_auto_read_progresses_and_does_not_add_todos(tmp_path, monkeypatch):
    monkeypatch.setattr("coursedeck.materials.MAX_BODY_READS", 2)
    calls = []

    async def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/toc"):
            return Response(index())
        if url.endswith("/news/"):
            return Response([{"Id": 8, "Title": "Notice", "Body": {"Text": "Announcement body"}}])
        return Response(detail(int(url.rsplit("/", 1)[1])))

    db, course, engine, collector = setup(tmp_path, get)
    # A first sync can supply source courses before applying them to the main DB.
    async with engine.queue_lock, engine.locks["brightspace"]:
        first = await collector.refresh_source("brightspace", courses=[course], already_locked=True)
        second = await collector.refresh_source(
            "brightspace", courses=[course], already_locked=True
        )
    assert any("await reading" in warning for warning in first["warnings"])
    deferred = next(doc for doc in first["documents"] if doc["id"] == "brightspace:1:content-13")
    assert deferred["complete"] is False and deferred["body"] == ""
    ids = {doc["id"] for doc in first["documents"] + second["documents"]}
    assert len(ids) == 5  # Four materials and one announcement, never duplicate Todo records.
    assert db.tasks() == []
    assert collector.knowledge.get("brightspace:1:content-13")["body"] == "Useful body 13"


@pytest.mark.asyncio
async def test_query_fetch_uses_selected_course_and_preserves_cached_docs_on_failure(tmp_path):
    async def get(url, **kwargs):
        return Response({}, 403)

    db, course, engine, collector = setup(tmp_path, get)
    db.apply(
        "brightspace", SyncResult(outcome=Outcome.SUCCESS, courses=[course]), now().isoformat()
    )
    collector.knowledge.upsert_documents(
        [
            {
                "id": "brightspace:1:content-11",
                "provider": "brightspace",
                "course_id": course.id,
                "title": "Notes",
                "body": "Cached",
            }
        ]
    )
    result = await collector.fetch(course.id, "Notes")
    assert not result["documents"] and result["warnings"]
    assert collector.knowledge.get("brightspace:1:content-11")["body"] == "Cached"


def test_course_scope_expands_workspace_binding_without_cross_course_leak(tmp_path):
    db = Database(tmp_path / "main.sqlite3")
    courses = [
        Course(provider="brightspace", external_id=str(i), name=f"Course{i}") for i in (1, 2, 3)
    ]
    db.apply("brightspace", SyncResult(outcome=Outcome.SUCCESS, courses=courses), now().isoformat())
    local = db.add_course(
        "Joined", "brightspace", courses[0].id, source_course_ids=[c.id for c in courses[:2]]
    )
    local_id = local["workspace_id"] if isinstance(local, dict) else local
    assert {c.id for c in source_courses(db, local_id)} == {c.id for c in courses[:2]}


def test_toc_rejects_ambiguous_or_truncated_structure():
    with pytest.raises(ValueError):
        toc_entries({"Objects": []})
    with pytest.raises(ValueError):
        toc_entries({"Modules": [{"ModuleId": 1, "Title": "Missing child lists"}]})
    data = index()
    data["Modules"][0]["Topics"].append(data["Modules"][0]["Topics"][0])
    with pytest.raises(ValueError):
        toc_entries(data)


def test_partial_attachment_refresh_keeps_full_cached_body_and_marks_it_stale(tmp_path):
    _, _, _, collector = setup(tmp_path, None)
    original = {
        "id": "google_classroom:1:material:2",
        "provider": "google_classroom",
        "course_id": "google_classroom:1",
        "title": "Syllabus",
        "body": "Previously extracted full attachment text",
        "complete": True,
        "fetched_at": "2026-09-01T00:00:00Z",
    }
    collector.knowledge.upsert_documents([original])
    result = {"documents": [], "warnings": []}
    collector.retain_document(
        original
        | {
            "body": "Current description with unread attachment link",
            "complete": False,
            "fetched_at": "2026-09-10T00:00:00Z",
            "checked_at": "2026-09-10T00:00:00Z",
            "warnings": ["Attachment unread"],
        },
        result,
    )
    saved = collector.knowledge.get(original["id"])
    assert saved["body"] == original["body"]
    assert saved["fetched_at"] == original["fetched_at"]
    assert saved["checked_at"] == "2026-09-10T00:00:00Z"
    assert saved["complete"] is False
    assert any("previous body retained" in warning for warning in saved["warnings"])


@pytest.mark.asyncio
async def test_classroom_facade_dispatches_to_active_browser_connector(tmp_path, monkeypatch):
    db = Database(tmp_path / "main.sqlite3")
    course = Course(provider="google_classroom", external_id="1", name="Writing")

    @asynccontextmanager
    async def session(timezone):
        async def new_page():
            return object()

        yield SimpleNamespace(new_page=new_page)

    active = SimpleNamespace(
        config={},
        browser=SimpleNamespace(session=session),
        display_name="Classroom",
        connection_status=lambda: "connected",
    )
    facade = SimpleNamespace(active=active, display_name="Classroom")
    engine = SimpleNamespace(
        connectors={"google_classroom": facade},
        queue_lock=asyncio.Lock(),
        locks={"google_classroom": asyncio.Lock()},
    )
    collector = MaterialCollector(db, engine, tmp_path)

    async def collect(connector, page, selected, on_document=None, **options):
        assert connector is active and selected.id == course.id
        return {"documents": [], "warnings": [], "complete": True}

    monkeypatch.setattr(
        "coursedeck.connectors.classroom_materials.collect_classroom_materials", collect
    )
    result = await collector.refresh_source("google_classroom", courses=[course])
    assert result == {"documents": [], "warnings": []}


@pytest.mark.asyncio
async def test_classroom_partial_read_is_saved_before_later_timeout(tmp_path, monkeypatch):
    db = Database(tmp_path / "main.sqlite3")
    course = Course(provider="google_classroom", external_id="1", name="Writing")

    @asynccontextmanager
    async def session(timezone):
        async def new_page():
            return object()

        yield SimpleNamespace(new_page=new_page)

    connector = SimpleNamespace(
        config={},
        browser=SimpleNamespace(session=session),
        display_name="Classroom",
        connection_status=lambda: "connected",
    )
    engine = SimpleNamespace(
        connectors={"google_classroom": connector},
        queue_lock=asyncio.Lock(),
        locks={"google_classroom": asyncio.Lock()},
    )
    collector = MaterialCollector(db, engine, tmp_path)
    document = {
        "id": "google_classroom:1:material:2",
        "provider": "google_classroom",
        "course_id": course.id,
        "title": "First material",
        "body": "Already read",
        "complete": True,
    }

    async def collect(connector, page, selected, on_document=None, **options):
        on_document(document)
        raise TimeoutError

    monkeypatch.setattr(
        "coursedeck.connectors.classroom_materials.collect_classroom_materials", collect
    )
    result = await collector.refresh_source("google_classroom", courses=[course])
    assert result["documents"][0]["id"] == document["id"]
    assert collector.knowledge.get(document["id"])["body"] == "Already read"
    assert any("timed out" in warning for warning in result["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("toc_has_stamp", [True, False])
async def test_cached_body_checks_refresh_without_changing_fetch_time(tmp_path, toc_has_stamp):
    stamp = detail(10)["LastModifiedDate"]

    async def get(url, **kwargs):
        if url.endswith("/toc"):
            data = index()
            data["Modules"][0]["Topics"] = []
            if toc_has_stamp:
                data["Modules"][0]["LastModifiedDate"] = stamp
            return Response(data)
        if url.endswith("/news/"):
            return Response([])
        return Response(detail(10))

    _, course, _, collector = setup(tmp_path, get)
    old_time = "2020-01-01T00:00:00Z"
    collector.knowledge.upsert_documents(
        [
            {
                "id": "brightspace:1:content-10",
                "provider": "brightspace",
                "course_id": course.id,
                "title": "Overview",
                "body": "Cached body",
                "complete": True,
                "source_modified_at": stamp,
                "fetched_at": old_time,
                "checked_at": old_time,
            }
        ]
    )
    result = await collector.refresh_source("brightspace", courses=[course])
    document = result["documents"][0]
    assert document["body"] == "Cached body" and document["complete"] is True
    assert document["fetched_at"] == old_time
    assert document["checked_at"] > old_time
    assert collector.knowledge.get(document["id"])["checked_at"] == document["checked_at"]


@pytest.mark.asyncio
async def test_unread_metadata_records_specific_error_on_document(tmp_path):
    async def get(url, **kwargs):
        if url.endswith("/toc"):
            return Response(index())
        return Response([]) if url.endswith("/news/") else Response({}, 403)

    _, course, _, collector = setup(tmp_path, get)
    result = await collector.refresh_source("brightspace", courses=[course])
    assert len(result["documents"]) == 4
    for document in result["documents"]:
        assert document["complete"] is False
        assert any("HTTP 403" in warning for warning in document["warnings"])
    assert any("HTTP 403" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_announcement_attachment_gap_keeps_current_body_and_visible_warning(tmp_path):
    async def get(url, **kwargs):
        if url.endswith("/toc"):
            return Response({"Modules": []})
        return Response(
            [
                {
                    "Id": 8,
                    "Title": "Notice",
                    "Body": {"Text": "Updated announcement"},
                    "Attachments": [{"FileId": 5}],
                }
            ]
        )

    _, course, _, collector = setup(tmp_path, get)
    collector.knowledge.upsert_documents(
        [
            {
                "id": "brightspace:1:news-8",
                "provider": "brightspace",
                "course_id": course.id,
                "title": "Notice",
                "body": "Old announcement",
                "complete": True,
            }
        ]
    )
    result = await collector.refresh_source("brightspace", courses=[course])
    document = result["documents"][0]
    assert document["body"] == "Updated announcement"
    assert document["complete"] is False
    assert any("attachments" in warning for warning in document["warnings"])
    assert any("attachments" in warning for warning in result["warnings"])
    assert collector.knowledge.get(document["id"])["body"] == "Updated announcement"


@pytest.mark.asyncio
async def test_all_restricted_content_is_not_reported_as_true_empty(tmp_path):
    async def get(url, **kwargs):
        if url.endswith("/toc"):
            data = index()
            module = data["Modules"][0]
            module["IsHidden"] = True
            module["IsLocked"] = True
            for topic in module["Topics"]:
                topic["IsLocked"] = True
            return Response(data)
        assert url.endswith("/news/")
        return Response([])

    _, course, _, collector = setup(tmp_path, get)
    result = await collector.refresh_source("brightspace", courses=[course])
    assert not result["documents"]
    assert any("4 hidden or locked" in warning for warning in result["warnings"])
