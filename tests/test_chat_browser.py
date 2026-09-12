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
                    status=content["status"], content_type="text/html", body=content["html"]
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
