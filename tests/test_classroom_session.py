from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from coursedeck.connectors import classroom_browser
from coursedeck.connectors.classroom_browser import ORIGIN, ClassroomBrowserConnector
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now

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


def redirect_home_to_accounts(context, monkeypatch):
    new_page = context.new_page

    async def redirected_page():
        page = await new_page()
        goto = page.goto

        async def redirected_goto(url, **options):
            # Playwright routing does not intercept subsequent HTTP redirect hops.
            # Start at the intercepted account page, then use real DOM navigation.
            return await goto(ACCOUNT if url == HOME else url, **options)

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
