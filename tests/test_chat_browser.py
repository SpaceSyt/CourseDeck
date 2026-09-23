import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_chat import configured, reply
from test_chat_tools import call

from coursedeck.browser import BrowserManager, installed_chrome_channel
from coursedeck.chat import ChatMessageInput
from coursedeck.chat_browser import BrowserTools
from coursedeck.chat_browser_policy import CourseNavigation, clean_url
from coursedeck.chat_tasks import TaskTools
from coursedeck.domain import Course, Outcome, SyncResult, Task, now

ROOT = "https://school.example/d2l/home/123"
TASK = "https://school.example/d2l/lms/dropbox/user/folder_submit_files.d2l?db=7&ou=123"
COURSE = {
    "id": "brightspace:123",
    "provider": "brightspace",
    "external_id": "123",
    "source_url": ROOT,
    "name": "Calculus",
}


@pytest.mark.parametrize(
    "url",
    [
        ROOT,
        TASK,
        "https://school.example/d2l/le/content/123/Home",
        "https://school.example/d2l/lms/quizzing/user/quiz_summary.d2l?qi=7&ou=123",
        "https://school.example/d2l/le/lessons/123",
        "https://school.example/d2l/le/lessons/123/topics/456",
        "https://school.example/d2l/le/lessons/123/units/456",
        "https://school.example/d2l/lms/news/main.d2l?ou=123",
        "https://school.example/d2l/lms/dropbox/dropbox.d2l?ou=123",
        "https://school.example/d2l/lms/quizzing/quizzing.d2l?ou=123",
    ],
)
def test_navigation_accepts_reading_pages(url):
    assert CourseNavigation(COURSE).allows(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://school.example/d2l/home/123",
        "https://school.example/d2l/home/999?ou=123",
        "https://school.example/d2l/home/123?ou=999&ou=123",
        "https://school.example/d2l/home/123?action=delete",
        "https://school.example/d2l/lms/quizzing/user/quiz_attempt.d2l?ou=123",
        "https://school.example/d2l/le/content/123/%2e%2e/999",
        "https://school.example.evil.test/d2l/home/123",
        "https://user:secret@school.example/d2l/home/123",
        "https://school.example/d2l/le/lessons/999/topics/456?ou=123",
        "https://school.example/d2l/le/lessons/123/topics/456/edit",
        "https://school.example/d2l/lms/news/main.d2l?ou=999",
        "https://school.example/d2l/lms/dropbox/dropbox.d2l",
    ],
)
def test_navigation_rejects_unsafe_pages(url):
    assert not CourseNavigation(COURSE).allows(url)


def test_provider_boundaries_and_webassign_credentials():
    root = "https://www.webassign.net/student?class=123"
    assignment = "https://www.webassign.net/web/Student/Assignment-Responses/last?dep=7"
    policy = CourseNavigation(COURSE | {"provider": "webassign", "source_url": root}, [assignment])
    assert policy.allows(assignment + "&UserPass=fixture-secret")
    assert clean_url(assignment + "&UserPass=fixture-secret") == assignment
    assert not policy.allows(assignment.replace("dep=7", "dep=8"))
    assert policy.allows(root + "&action=pastassignments")
    classroom = CourseNavigation(
        COURSE
        | {"provider": "google_classroom", "source_url": "https://classroom.google.com/c/abc"}
    )
    assert classroom.allows("https://classroom.google.com/u/0/c/abc/a/xyz/details")
    assert not classroom.allows("https://classroom.google.com/c/other")
    grade = CourseNavigation(
        COURSE | {"provider": "gradescope", "source_url": "https://www.gradescope.com/courses/123"}
    )
    assert grade.allows("https://www.gradescope.com/courses/123/assignments/7/submissions/8")
    assert not grade.allows("https://www.gradescope.com/courses/123/assignments/7/submissions/new")


HTML = """<html><head><title>Calculus materials</title></head><body>
<main><h1>Assignment requirements</h1><p>Write a proof and cite the theorem.</p>
<details><summary>Instructions</summary><p>Show every intermediate step.</p></details>
<input type="hidden" value="fixture-secret"><textarea>PRIVATE INPUT</textarea>
<span hidden>HIDDEN TEXT</span><div id="shadow"></div>
<a href="/d2l/home/999">Other course</a><a href="/d2l/le/content/123/delete">Delete</a>
<a href="https://docs.google.com/document/d/abc/edit">Rubric document</a>
<button type="submit">Submit</button>
<button onclick="fetch('/d2l/home/123', {method:'POST',body:'write'})">Next page</button>
<script>document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML =
  '<p>Shadow-root instructions</p>';</script>
__LINKS__</main></body></html>""".replace(
    "__LINKS__",
    "".join(
        f'<a href="/d2l/le/content/123/viewContent/{n}/View">Material {n}</a>' for n in range(50)
    ),
)


@pytest.fixture
async def case(tmp_path, monkeypatch):
    async def public(_):
        return "93.184.216.34"

    monkeypatch.setattr("coursedeck.chat_browser.public_address", public)
    service = await configured(tmp_path, lambda _: reply())
    course = Course(provider="brightspace", external_id="123", name="Calculus", source_url=ROOT)
    task = Task(
        provider="brightspace", external_id="7", course_external_id="123", title="Proof", url=TASK
    )
    service.db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    manager = BrowserManager(tmp_path, "brightspace", channel=installed_chrome_channel())
    contexts, received = [], []
    content = {"html": HTML, "status": 200}
    original_session = manager.session

    @asynccontextmanager
    async def session(timezone=None, *, read_only=False):
        assert read_only
        async with original_session(timezone, read_only=read_only) as context:
            contexts.append(context)

            async def serve(route):
                received.append((route.request.method, route.request.url))
                await route.fulfill(
                    status=content["status"],
                    content_type="text/html; charset=utf-8",
                    body=content["html"],
                )

            await context.route("**/*", serve)
            yield context

    manager.session = session
    connector = SimpleNamespace(
        browser=manager,
        connection_status=lambda: "connected",
        config={},
        base_url="https://school.example",
    )
    service.engine = SimpleNamespace(
        connectors={"brightspace": connector},
        paused=False,
        queue_lock=asyncio.Lock(),
        locks={"brightspace": asyncio.Lock()},
    )
    value = ChatMessageInput(message="Find the rubric", course_id="brightspace:123")

    def expose(docs, **_):
        return docs

    tasks = TaskTools(service, ["brightspace:123"], value, expose)
    tools = BrowserTools(service, tasks, expose)
    try:
        yield SimpleNamespace(
            tools=tools,
            service=service,
            contexts=contexts,
            content=content,
            received=received,
            manager=manager,
        )
    finally:
        await tools.close()
        await manager.close()


async def test_real_headless_snapshot_pagination_and_cleanup(case):
    async with case.tools:
        opened = await case.tools.run("browser_open", {"task_id": "brightspace:123:7"})
        document = opened["documents"][0]
        assert "Write a proof" in document["body"]
        assert "Show every intermediate step" not in document["body"]
        assert "Shadow-root instructions" in document["body"]
        assert "HIDDEN TEXT" not in document["body"] and "PRIVATE INPUT" not in document["body"]
        assert "fixture-secret" not in json.dumps(opened)
        assert document["complete"] is False and document["checked_at"]
        assert opened["total_targets"] > 40
        second = await case.tools.run(
            "browser_read", {"snapshot_id": opened["snapshot_id"], "target_offset": 40}
        )
        assert second["targets"] and second["targets"][0] != opened["targets"][0]
        assert not {"Submit", "Delete", "Other course"} & {t["label"] for t in opened["targets"]}
        target = next(t for t in opened["targets"] if t["label"] == "Instructions")
        expanded = await case.tools.run(
            "browser_follow",
            {"snapshot_id": opened["snapshot_id"], "target_id": target["target_id"]},
        )
        assert "Show every intermediate step" in expanded["documents"][0]["body"]
        material = next(t for t in expanded["targets"] if t["label"] == "Material 0")
        followed = await case.tools.run(
            "browser_follow",
            {"snapshot_id": expanded["snapshot_id"], "target_id": material["target_id"]},
        )
        assert followed["browser_visit"]["url"].endswith("/0/View")
    assert not case.contexts[-1].pages
    assert not case.service.engine.queue_lock.locked()
    assert not case.service.engine.locks["brightspace"].locked()


async def test_post_blocked_and_stale_targets_rejected(case):
    opened = await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    target = next(t for t in opened["targets"] if t["label"] == "Next page")
    result = await case.tools.run(
        "browser_follow", {"snapshot_id": opened["snapshot_id"], "target_id": target["target_id"]}
    )
    assert any("dynamic requests" in w for w in result["browser_visit"]["warnings"])
    assert all(method == "GET" for method, _ in case.received)
    with pytest.raises(ValueError, match="Page changed"):
        await case.tools.run(
            "browser_follow",
            {"snapshot_id": opened["snapshot_id"], "target_id": target["target_id"]},
        )
    assert not case.service.engine.queue_lock.locked()


async def test_scope_and_login_failure_never_open_interactive_browser(case):
    with pytest.raises(ValueError, match="outside"):
        await case.tools.run("browser_open", {"source_course_id": "test:writing"})
    assert not case.contexts
    case.content["html"] = '<input type="password"><h1>Sign in</h1>'
    with pytest.raises(ValueError, match="sign-in"):
        await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert case.manager.interactive is None and not case.service.engine.queue_lock.locked()


async def test_brightspace_restores_fixed_session_before_restricting_course_navigation(case):
    restored = []

    async def restore(context):
        assert case.service.engine.queue_lock.locked()
        assert case.service.engine.locks["brightspace"].locked()
        page = await context.new_page()
        await page.goto("https://school.example/d2l/home")
        await page.evaluate("sessionStorage.setItem('fixture-sign-in', 'restored')")
        await page.evaluate("fetch('/d2l/lp/auth/login/samlLogin.d2l', {method:'POST'})")
        restored.append(page)

    case.service.engine.connectors["brightspace"].restore_browser_session = restore
    result = await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert len(restored) == 1 and case.tools.page is restored[0]
    assert await case.tools.page.evaluate("sessionStorage.getItem('fixture-sign-in')") == "restored"
    assert result["browser_visit"]["url"] == ROOT
    assert ("POST", "https://school.example/d2l/lp/auth/login/samlLogin.d2l") in case.received
    before = len(case.received)
    target = next(t for t in result["targets"] if t["label"] == "Next page")
    followed = await case.tools.run(
        "browser_follow",
        {
            "snapshot_id": result["snapshot_id"],
            "target_id": target["target_id"],
        },
    )
    assert all(method == "GET" for method, _ in case.received[before:])
    assert any("dynamic requests" in warning for warning in followed["browser_visit"]["warnings"])
    with pytest.raises(ValueError, match="outside"):
        await case.tools.run("browser_open", {"source_course_id": "brightspace:999"})
    assert len(restored) == 1


async def test_brightspace_session_restore_failure_is_clear_and_releases_profile(case):
    async def restore(context):
        await context.new_page()
        raise ValueError("https://idp.example/login?token=fixture-secret")

    case.service.engine.connectors["brightspace"].restore_browser_session = restore
    with pytest.raises(ValueError, match="sign-in could not be restored") as error:
        await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert "fixture-secret" not in str(error.value)
    assert not case.received and not case.contexts[-1].pages
    assert not case.service.engine.queue_lock.locked()


async def test_blocked_signin_redirect_is_not_reported_as_another_course(case):
    async def navigate(_):
        await case.tools.page.goto("https://school.example/d2l/login")

    case.tools.navigate = navigate
    with pytest.raises(ValueError, match="redirected to sign-in"):
        await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert not case.service.engine.queue_lock.locked()
    with pytest.raises(ValueError, match="outside"):
        await case.tools.run("browser_open", {"source_course_id": "brightspace:999"})


async def test_embedded_signin_failure_keeps_readable_course_evidence(case):
    case.content["html"] += '<iframe src="https://school.example/d2l/login"></iframe>'
    result = await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert "Write a proof" in result["documents"][0]["body"]
    assert any("embedded frame" in warning for warning in result["browser_visit"]["warnings"])


@pytest.mark.parametrize("metadata_change", [{}, {"Id": 999}, {"IsHidden": True}])
async def test_cached_brightspace_topic_reads_confirmed_static_attachment(
    case, monkeypatch, metadata_change
):
    document = {
        "id": "material:syllabus",
        "provider": "brightspace",
        "course_id": "brightspace:123",
        "title": "Syllabus",
        "body": "Cached excerpt",
        "url": (
            "https://school.example/d2l/le/content/123/Home"
            "?itemIdentifier=D2L.LE.Content.ContentObject.TopicCO-456"
        ),
    }
    case.service.store.upsert_documents([document])
    case.tools.documents[document["id"]] = document
    calls = []

    async def restore(context):
        page = await context.new_page()
        await page.goto("https://school.example/d2l/home")

        async def metadata():
            return {
                "Id": 456,
                "Type": 1,
                "IsHidden": False,
                "IsLocked": False,
                "Title": "Current syllabus",
                "Url": "/content/enforced/123-course/syllabus.pdf",
                **metadata_change,
            }

        async def get(url, **kwargs):
            calls.append(url)
            assert kwargs["max_redirects"] == 0
            return SimpleNamespace(status=200, json=metadata)

        monkeypatch.setattr(context.request, "get", get)

    async def read(task, link):
        assert not case.service.engine.queue_lock.locked() and not case.contexts[-1].pages
        assert link["url"] == "https://school.example/content/enforced/123-course/syllabus.pdf"
        return document | {
            "id": "link:current",
            "url": link["url"],
            "title": link["title"],
            "body": "Weekly quizzes cover the prior week",
            "complete": True,
            "checked_at": now().isoformat(),
            "warnings": [],
        }

    case.service.engine.connectors["brightspace"].restore_browser_session = restore
    case.service.link_reader = SimpleNamespace(read=read)
    if metadata_change:
        with pytest.raises(ValueError, match="identity or visibility"):
            await case.tools.run("browser_open", {"document_id": document["id"]})
        assert not case.service.engine.queue_lock.locked()
    else:
        result = await case.tools.run("browser_open", {"document_id": document["id"]})
        assert result["documents"][0]["body"] == "Weekly quizzes cover the prior week"
        assert result["documents"][0]["title"] == "Current syllabus"
        assert not result["targets"]
    assert calls == ["https://school.example/d2l/api/le/1.82/123/content/topics/456"]
    assert case.service.store.get(document["id"])["body"] == "Cached excerpt"


async def test_loading_page_shell_is_explicitly_incomplete(case):
    case.content["html"] = "<title>Loading... - Calculus</title><h1>Calculus</h1>"
    result = await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert any("page shell" in warning for warning in result["browser_visit"]["warnings"])
    assert result["documents"][0]["complete"] is False


async def test_cancel_while_queued_releases_only_owned_locks(case):
    await case.service.engine.queue_lock.acquire()
    job = asyncio.create_task(
        case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    )
    await asyncio.sleep(0.05)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert case.service.engine.queue_lock.locked() and not case.contexts
    case.service.engine.queue_lock.release()


async def test_cancel_with_open_profile_closes_context_and_preserves_tasks(case):
    before = case.service.db.tasks()
    await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    waiting = asyncio.Event()

    async def snapshot():
        waiting.set()
        await asyncio.Event().wait()

    case.tools.snapshot = snapshot
    job = asyncio.create_task(
        case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    )
    await waiting.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert not case.contexts[-1].pages and not case.service.engine.queue_lock.locked()
    assert case.service.db.tasks() == before


async def test_failed_page_and_action_limit_preserve_evidence(case):
    before = case.service.db.tasks()
    case.content["status"] = 403
    with pytest.raises(ValueError, match="unavailable"):
        await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert not case.contexts[-1].pages and not case.service.engine.queue_lock.locked()
    case.tools.operations = 10
    with pytest.raises(ValueError, match="limit"):
        await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert case.service.db.tasks() == before


async def test_attachment_releases_profile_before_existing_reader(case):
    opened = await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    target = next(t for t in opened["targets"] if t["label"] == "Rubric document")

    async def read(task, link):
        assert not case.service.engine.queue_lock.locked() and not case.contexts[-1].pages
        return {
            "id": "link:rubric",
            "title": link["title"],
            "url": link["url"],
            "body": "Evidence criteria",
            "course_id": "brightspace:123",
            "checked_at": now().isoformat(),
            "warnings": [],
            "complete": True,
        }

    case.service.link_reader = SimpleNamespace(read=read)
    result = await case.tools.run(
        "browser_follow", {"snapshot_id": opened["snapshot_id"], "target_id": target["target_id"]}
    )
    assert result["documents"][0]["body"] == "Evidence criteria" and result["targets"] == []


async def test_discovers_uncached_public_document_inside_named_module(case):
    case.content["html"] = """<h1>Course content</h1>
    <details><summary>Week 3 — Integrals</summary>
      <a href="https://readings.example/lecture.pdf">Lecture notes</a>
    </details>
    <a href="https://readings.example/video">Lecture video</a>
    <a href="https://school.example/content/enforced/999/other.pdf">Other course file</a>
    <a href="https://readings.example/remove/file.pdf">Remove file</a>
    <a href="https://readings.example/file.pdf?token=fixture-secret">Protected link</a>
    <button type="button" aria-controls="lesson" aria-expanded="false">Lecture 2</button>"""
    opened = await case.tools.run("browser_open", {"source_course_id": "brightspace:123"})
    assert "Lecture notes" not in {item["label"] for item in opened["targets"]}
    assert any("visible links" in warning for warning in opened["browser_visit"]["warnings"])
    assert any("visible controls" in warning for warning in opened["browser_visit"]["warnings"])
    module = next(t for t in opened["targets"] if t["label"] == "Week 3 — Integrals")
    expanded = await case.tools.run(
        "browser_follow",
        {
            "snapshot_id": opened["snapshot_id"],
            "target_id": module["target_id"],
        },
    )
    labels = {item["label"] for item in expanded["targets"]}
    assert not {"Other course file", "Remove file", "Protected link"} & labels
    target = next(t for t in expanded["targets"] if t["label"] == "Lecture notes")
    assert target["kind"] == "document"

    async def read(task, link):
        assert not case.service.engine.queue_lock.locked()
        assert not case.contexts[-1].pages
        assert task["source_course_id"] == "brightspace:123"
        assert link["url"] == "https://readings.example/lecture.pdf"
        return {
            "id": "link:lecture",
            "title": link["title"],
            "url": link["url"],
            "body": "Integration by parts",
            "course_id": "brightspace:123",
            "source_task_id": task["id"],
            "checked_at": now().isoformat(),
            "warnings": [],
            "complete": True,
        }

    case.service.link_reader = SimpleNamespace(read=read)
    result = await case.tools.run(
        "browser_follow",
        {
            "snapshot_id": expanded["snapshot_id"],
            "target_id": target["target_id"],
        },
    )
    assert result["documents"][0]["body"] == "Integration by parts"
    assert "source_task_id" not in result["documents"][0]
    assert not case.service.store.get("link:lecture")


async def test_open_cached_material_source_requires_exposed_id_and_active_scope(case):
    document = {
        "id": "material:proof",
        "provider": "brightspace",
        "course_id": "brightspace:123",
        "title": "Proof notes",
        "body": "Partial notes",
        "complete": False,
        "url": "https://school.example/d2l/le/content/123/viewContent/456/View",
    }
    case.service.store.upsert_documents([document])
    with pytest.raises(ValueError, match="returned in this question"):
        await case.tools.run("browser_open", {"document_id": document["id"]})
    assert not case.contexts
    case.tools.documents[document["id"]] = document
    result = await case.tools.run("browser_open", {"document_id": document["id"]})
    assert result["browser_visit"]["url"] == document["url"]
    assert case.received[-1][1] == document["url"]

    case.service.store.upsert_documents([document | {"course_id": "brightspace:999"}])
    with pytest.raises(ValueError, match="outside"):
        await case.tools.run("browser_open", {"document_id": document["id"]})
    assert not case.service.engine.queue_lock.locked()


async def test_open_cached_attachment_hands_off_without_opening_browser(case):
    document = {
        "id": "material:handout",
        "provider": "brightspace",
        "course_id": "brightspace:123",
        "title": "Handout",
        "body": "",
        "complete": False,
        "url": "https://readings.example/handout.txt",
    }
    case.service.store.upsert_documents([document])
    case.tools.documents[document["id"]] = document

    async def read(task, link):
        assert not case.contexts and not case.service.engine.queue_lock.locked()
        assert link["url"] == document["url"]
        return document | {
            "body": "The uncached handout",
            "checked_at": now().isoformat(),
            "warnings": [],
            "complete": True,
        }

    case.service.link_reader = SimpleNamespace(read=read)
    result = await case.tools.run("browser_open", {"document_id": document["id"]})
    assert result["documents"][0]["body"] == "The uncached handout"
    assert result["targets"] == []
    case.service.engine.paused = True
    with pytest.raises(ValueError, match="paused"):
        await case.tools.run("browser_open", {"document_id": document["id"]})


async def test_chat_loop_persists_browser_sources_and_closes_browser(case):
    def model(request):
        payload = json.loads(request.content)
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not outputs:
            assert "browser_open" in [t["function"]["name"] for t in payload["tools"]]
            return reply(None, [call("browser_open", task_id="brightspace:123:7")])
        doc = outputs[-1]["documents"][0]
        return reply("Use the theorem [[" + doc["id"] + "]].")

    case.service.transport = httpx.MockTransport(model)
    result = await case.service.send(
        ChatMessageInput(
            message="Check the assignment requirements in the browser", course_id="brightspace:123"
        )
    )
    assistant = case.service.store.conversation(result["conversation_id"])["messages"][-1]
    assert assistant["citations"][0]["url"] == TASK
    assert any(item.get("browser_visit") for item in assistant["activity"])
    assert not case.contexts[-1].pages and not case.service.engine.queue_lock.locked()
