import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import async_playwright

from coursedeck.connectors import classroom_browser
from coursedeck.connectors.classroom_browser import ORIGIN, ClassroomBrowserConnector
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.sync import SyncEngine

HOME = ORIGIN + "/u/0/h"
ACCOUNT = "https://accounts.google.com/synthetic-session"
COURSE = """<ul><li data-course-id="123"><h2>Computing</h2>
<a href="https://classroom.google.com/c/MTIz">Computing</a></li></ul>"""


@pytest.fixture
async def session_connector(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    db.update_state("google_classroom", authorized=True)
    task = Task(
        provider="google_classroom",
        external_id="456",
        course_external_id="123",
        title="Cached homework",
        description="Previously read instructions",
        url=ORIGIN + "/c/MTIz/a/NDU2/details",
    )
    db.apply(
        "google_classroom",
        SyncResult(
            outcome=Outcome.SUCCESS,
            courses=[Course(provider="google_classroom", external_id="123", name="Computing")],
            tasks=[task],
        ),
        now().isoformat(),
    )
    db.patch_local(task.id, {"note": "Keep this local note"})
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()

        class Session:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                yield context

        connector = ClassroomBrowserConnector(db, Session())
        try:
            yield connector, context
        finally:
            await browser.close()


def redirect_home_to_accounts(context, monkeypatch, account_url=ACCOUNT):
    new_page = context.new_page

    async def redirected_page():
        page = await new_page()
        goto = page.goto

        async def redirected_goto(url, **options):
            # Playwright routing does not intercept subsequent HTTP redirect hops.
            # Start at the intercepted account page, then use real DOM navigation.
            return await goto(account_url if url == HOME else url, **options)

        monkeypatch.setattr(page, "goto", redirected_goto)
        return page

    monkeypatch.setattr(context, "new_page", redirected_page)


@pytest.mark.parametrize("account_status", [200, 403])
async def test_school_session_redirect_returns_to_classroom(
    session_connector, monkeypatch, account_status
):
    connector, context = session_connector
    visited = []
    redirect_home_to_accounts(context, monkeypatch)

    async def route(request):
        url = request.request.url
        visited.append(url)
        if url == ACCOUNT:
            await request.fulfill(
                status=account_status,
                content_type="text/html",
                body=f"<script>setTimeout(() => location.href = '{HOME}?renewed=1', 150)</script>",
            )
        elif url == HOME + "?renewed=1":
            await request.fulfill(content_type="text/html", body=COURSE)
        elif url == HOME + "/archived":
            await request.fulfill(content_type="text/html", body="No classes")
        else:
            await request.abort()

    await context.route("**/*", route)
    monkeypatch.setattr(connector, "expand_list", AsyncMock())
    monkeypatch.setattr(connector, "read_course", AsyncMock())
    result = await connector.sync()
    assert result.outcome == Outcome.PARTIAL  # Browser coverage remains explicitly partial.
    assert [course.id for course in result.courses] == ["google_classroom:123"]
    assert result.warnings == []
    assert connector.db.state(connector.key)["authorized"]
    assert ACCOUNT in visited and HOME + "?renewed=1" in visited


@pytest.mark.parametrize(
    ("failure", "outcome", "authorized"),
    [
        ("signin", Outcome.AUTH_REQUIRED, False),
        ("redirect_loading", Outcome.NETWORK_ERROR, True),
        ("permission", Outcome.PARSE_ERROR, True),
        ("timeout", Outcome.NETWORK_ERROR, True),
        ("offline", Outcome.NETWORK_ERROR, True),
    ],
)
async def test_session_failures_preserve_cached_tasks(
    session_connector, monkeypatch, failure, outcome, authorized
):
    connector, context = session_connector
    monkeypatch.setattr(classroom_browser, "SESSION_REDIRECT_TIMEOUT_MS", 150)
    if failure in {"signin", "redirect_loading"}:
        redirect_home_to_accounts(context, monkeypatch)

    async def route(request):
        if failure == "signin":
            await request.fulfill(content_type="text/html", body='<input type="password">')
        elif failure == "redirect_loading":
            await request.fulfill(content_type="text/html", body="Loading")
        elif failure == "permission":
            await request.fulfill(status=403, content_type="text/html", body="Access denied")
        else:
            await request.abort("timedout" if failure == "timeout" else "internetdisconnected")

    await context.route("**/*", route)
    with connector.db.connection() as db:
        before_tasks = [tuple(row) for row in db.execute("SELECT * FROM tasks")]
        before_local = [tuple(row) for row in db.execute("SELECT * FROM task_local_states")]
    result = await connector.sync()
    assert result.outcome == outcome
    assert result.warnings and not result.complete
    assert connector.db.state(connector.key)["authorized"] is authorized
    connector.db.apply(connector.key, result, now().isoformat())
    with connector.db.connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM tasks")] == before_tasks
        assert [tuple(row) for row in db.execute("SELECT * FROM task_local_states")] == before_local


async def test_delayed_google_next_redirect_recovers_stale_disconnection(
    session_connector, monkeypatch
):
    connector, context = session_connector
    connector.db.update_state(connector.key, authorized=False, last_outcome=Outcome.AUTH_REQUIRED)
    confirmation = "https://accounts.google.com/v3/signin/confirmidentifier"
    clicks = []
    await context.expose_function("nextClicked", lambda: clicks.append("next"))

    async def route(request):
        url = request.request.url
        if url == HOME:
            body = (
                "<main>Loading</main><script>setTimeout(()=>location.href="
                + json.dumps(confirmation)
                + ",100)</script>"
            )
        elif url == confirmation:
            body = (
                '<div data-profile-identifier data-email="student@example.edu">Account</div>'
                '<div id="identifierNext"><button onclick="nextClicked();'
                f"location.href='{HOME}?renewed=1'\">Next</button></div>"
            )
        elif url == HOME + "?renewed=1":
            body = COURSE
        else:
            body = "No classes"
        await request.fulfill(content_type="text/html", body=body)

    await context.route("**/*", route)
    monkeypatch.setattr(connector, "expand_list", AsyncMock())
    monkeypatch.setattr(connector, "read_course", AsyncMock())
    engine = SyncEngine(connector.db, [connector])
    try:
        await engine._sync_connected(connector.key, automatic=True)
        assert clicks == ["next"]
        assert connector.connection_status() == "connected"
        assert connector.db.state(connector.key)["last_outcome"] == Outcome.PARTIAL
        assert not connector.db.state(connector.key)["session_recovery_attempted"]
        assert connector.db.tasks()[0]["local"]["note"] == "Keep this local note"
    finally:
        # The fixture owns the synthetic context; this minimal manager has no close method.
        await asyncio.gather(*engine.jobs)


@pytest.mark.parametrize("challenge", ['<input type="password">', '<input name="totpPin">'])
async def test_stale_recovery_stops_after_one_human_challenge(
    session_connector, monkeypatch, challenge
):
    connector, context = session_connector
    connector.db.update_state(connector.key, authorized=False, last_outcome=Outcome.AUTH_REQUIRED)
    monkeypatch.setattr(classroom_browser, "SESSION_REDIRECT_TIMEOUT_MS", 150)
    redirect_home_to_accounts(context, monkeypatch)
    visits = []

    async def route(request):
        visits.append(request.request.url)
        await request.fulfill(content_type="text/html", body=challenge + "<button>Next</button>")

    await context.route("**/*", route)
    engine = SyncEngine(connector.db, [connector])
    await engine._sync_connected(connector.key, automatic=True)
    assert connector.db.state(connector.key)["session_recovery_attempted"]
    assert connector.db.state(connector.key)["metadata"]["session_recovery"]["category"] in {
        "credentials_required",
        "mfa_required",
    }
    assert not connector.can_resume_session()
    count = len(visits)
    await engine._sync_connected(connector.key, automatic=True)
    assert len(visits) == count
    assert connector.db.tasks()[0]["local"]["note"] == "Keep this local note"


async def test_disconnect_or_interactive_login_never_qualifies_for_recovery(session_connector):
    connector, context = session_connector
    connector.db.update_state(connector.key, authorized=False, last_outcome=None)
    assert not connector.can_resume_session()
    connector.db.update_state(connector.key, last_outcome=Outcome.AUTH_REQUIRED)
    connector.browser.interactive = context
    assert not connector.can_resume_session()


@pytest.mark.parametrize("missing", ["profile", "courses"])
async def test_recovery_requires_existing_profile_and_course_cache(
    session_connector, monkeypatch, missing
):
    connector, _ = session_connector
    connector.db.update_state(connector.key, authorized=False, last_outcome=Outcome.AUTH_REQUIRED)
    if missing == "profile":
        monkeypatch.setattr(connector.browser, "exists", Mock(return_value=False))
    else:
        monkeypatch.setattr(connector.db, "source_courses", Mock(return_value=[]))
    assert not connector.can_resume_session()


@pytest.mark.parametrize("change", ["disconnect", "interactive"])
async def test_queued_recovery_rechecks_user_connection_change(
    session_connector, monkeypatch, change
):
    connector, context = session_connector
    connector.db.update_state(connector.key, authorized=False, last_outcome=Outcome.AUTH_REQUIRED)
    read = AsyncMock()
    monkeypatch.setattr(connector, "read_course_list", read)
    engine = SyncEngine(connector.db, [connector])
    await engine.queue_lock.acquire()
    queued = asyncio.create_task(engine._sync_connected(connector.key, automatic=True))
    await asyncio.sleep(0)
    assert connector.key in engine.pending
    if change == "disconnect":
        connector.db.update_state(connector.key, last_outcome=None)
    else:
        connector.browser.interactive = context
    engine.queue_lock.release()
    await queued
    read.assert_not_awaited()
    assert not engine.pending and not engine.locks[connector.key].locked()


async def test_stalled_safe_next_is_transient_not_revoked_login(session_connector, monkeypatch):
    connector, context = session_connector
    monkeypatch.setattr(classroom_browser, "SESSION_REDIRECT_TIMEOUT_MS", 700)
    confirmation = "https://accounts.google.com/v3/signin/confirmidentifier"
    redirect_home_to_accounts(context, monkeypatch, confirmation)
    clicks = []
    await context.expose_function("nextClicked", lambda: clicks.append("next"))
    await context.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html",
            body='<div data-profile-identifier data-email="student@example.edu">Account</div>'
            '<div id="identifierNext"><button onclick="nextClicked()">Next</button></div>',
        ),
    )
    result = await connector.sync()
    assert result.outcome == Outcome.NETWORK_ERROR
    assert connector.db.state(connector.key)["authorized"]
    assert clicks == ["next"]


async def test_cancelled_recovery_releases_queue_and_can_be_retried(session_connector, monkeypatch):
    connector, _ = session_connector
    connector.db.update_state(connector.key, authorized=False, last_outcome=Outcome.AUTH_REQUIRED)
    started = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(connector, "read_course_list", pending)
    engine = SyncEngine(connector.db, [connector])
    first = asyncio.create_task(engine._sync_connected(connector.key, automatic=True))
    await started.wait()
    await engine._sync_connected(connector.key, automatic=True)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not connector.db.state(connector.key)["session_recovery_attempted"]
    assert not engine.pending and not engine.running and not engine.locks[connector.key].locked()
    assert connector.can_resume_session()
    assert connector.db.tasks()[0]["local"]["note"] == "Keep this local note"
