import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import async_playwright

from coursedeck.connectors import gradescope, webassign
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now

GRADE_HOME = "https://www.gradescope.com/account"
WEB_HOME = "https://www.webassign.net/v4cgi/student.pl?course=123"
WEB_DOCUMENT = """<main id="js-student-myAssignmentsPage">
<h1>My Assignments</h1><a id="backNavBtn">Calculus</a>
<button role="tab" aria-label="Show All Assignments (selected)">All</button>
<table><thead><tr><th>Current Assignments</th><th>Due Date</th><th>Status</th></tr>
</thead><tbody><tr><th><a href="/assignment?dep=456">Homework</a></th>
<td>Friday, September 18, 2026 at 11:59 PM EDT</td><td>Submitted</td></tr></tbody>
</table><p>There are no past assignments.</p></main>"""
GRADE_DOCUMENT = """<table id="assignments-student-table"><tbody><tr><th>
<a href="/courses/123/assignments/456">Homework</a></th><td>Submitted</td><td>
<time class="submissionTimeChart--dueDate" datetime="2026-09-19T03:59:00Z"></time>
</td></tr></tbody></table>"""


class Vault:
    def __init__(self):
        self.value = {"url": WEB_HOME + "&UserPass=synthetic-old"}

    def get(self, key):
        return dict(self.value)

    def set(self, key, value):
        self.value = dict(value)


@pytest.fixture
async def source_session(tmp_path, monkeypatch):
    db = Database(tmp_path / "tasks.sqlite3")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()

        class Manager:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                yield context

        for module in (gradescope, webassign):
            original = module.resume_session

            async def bounded(page, ready, _original=original):
                return await _original(page, ready, timeout_ms=1000)

            monkeypatch.setattr(module, "resume_session", bounded)
        try:
            yield db, context, Manager()
        finally:
            await browser.close()


def redirect_initial_navigation(context, monkeypatch, home, entry):
    new_page = context.new_page
    redirected = False

    async def make_page():
        page = await new_page()
        goto = page.goto

        async def navigate(url, **options):
            nonlocal redirected
            if not redirected and url.startswith(home):
                redirected = True
                url = entry
            return await goto(url, **options)

        monkeypatch.setattr(page, "goto", navigate)
        return page

    monkeypatch.setattr(context, "new_page", make_page)


async def test_gradescope_existing_sso_returns_before_course_read(source_session, monkeypatch):
    db, context, manager = source_session
    db.update_state("gradescope", authorized=True)
    entry = "https://accounts.google.com/synthetic-session"
    redirect_initial_navigation(context, monkeypatch, GRADE_HOME, entry)
    visited = []

    async def respond(route):
        url = route.request.url
        visited.append(urlparse(url).hostname)
        if url == entry:
            body = f"<script>setTimeout(()=>location.href={json.dumps(GRADE_HOME)},100)</script>"
        elif url == GRADE_HOME:
            body = '<a href="/courses/123">Computing</a>'
        else:
            body = GRADE_DOCUMENT
        await route.fulfill(body=body, content_type="text/html")

    await context.route("**/*", respond)
    result = await gradescope.GradescopeConnector(db, manager).sync()
    assert result.outcome == Outcome.SUCCESS
    assert [task.external_id for task in result.tasks] == ["456"]
    assert result.tasks[0].submission_status == "submitted"
    assert "accounts.google.com" in visited


@pytest.mark.parametrize(
    ("entry", "body", "outcome"),
    [
        (GRADE_HOME, '<input type="password"><button>Next</button>', Outcome.AUTH_REQUIRED),
        (
            "https://www.gradescope.com/login",
            '<input type="password"><button>Next</button>',
            Outcome.AUTH_REQUIRED,
        ),
        ("https://accounts.google.com/synthetic-session", "Loading", Outcome.NETWORK_ERROR),
    ],
)
async def test_gradescope_failed_resume_retains_cache(
    source_session, monkeypatch, entry, body, outcome
):
    db, context, manager = source_session
    db.update_state("gradescope", authorized=True)
    old = Task(provider="gradescope", external_id="456", course_external_id="123", title="Old")
    db.apply(
        "gradescope",
        SyncResult(
            outcome=Outcome.SUCCESS,
            courses=[Course(provider="gradescope", external_id="123", name="Computing")],
            tasks=[old],
        ),
        now().isoformat(),
    )
    db.patch_local(old.id, {"note": "Keep this note"})
    redirect_initial_navigation(context, monkeypatch, GRADE_HOME, entry)
    await context.route(
        "**/*",
        lambda route: route.fulfill(body=body, content_type="text/html"),
    )
    result = await gradescope.GradescopeConnector(db, manager).sync()
    assert result.outcome == outcome
    assert result.tasks == [] and result.covered_task_scopes == []
    assert db.tasks()[0]["id"] == old.id


async def test_webassign_redirect_rotates_vault_token_before_next_sync(source_session, monkeypatch):
    db, context, manager = source_session
    db.update_state("webassign", authorized=True, landing_page=WEB_HOME)
    vault = Vault()
    entry = "https://account.cengage.com/synthetic-session"
    renewed = WEB_HOME + "&UserPass=synthetic-renewed"
    redirect_initial_navigation(context, monkeypatch, WEB_HOME, entry)
    tokens = []

    async def respond(route):
        url = route.request.url
        if url == entry:
            body = f"<script>setTimeout(()=>location.href={json.dumps(renewed)},100)</script>"
        else:
            tokens.extend(parse_qs(urlparse(url).query).get("UserPass", []))
            body = WEB_DOCUMENT
        await route.fulfill(body=body, content_type="text/html")

    await context.route("**/*", respond)
    connector = webassign.WebAssignConnector(db, manager, vault)
    first = await connector.sync()
    assert first.outcome == Outcome.SUCCESS and first.tasks[0].external_id == "456"
    assert parse_qs(urlparse(vault.value["url"]).query)["UserPass"] == ["synthetic-renewed"]
    assert "UserPass" not in db.state("webassign")["landing_page"]
    second = await connector.sync()
    assert second.outcome == Outcome.SUCCESS
    assert tokens == ["synthetic-renewed", "synthetic-renewed"]


@pytest.mark.parametrize(
    ("entry", "body", "outcome"),
    [
        (
            "https://www.webassign.net/login.html",
            '<input type="password"><button>Next</button>',
            Outcome.AUTH_REQUIRED,
        ),
        (WEB_HOME.replace("course=123", "course=999"), WEB_DOCUMENT, Outcome.PARSE_ERROR),
        ("https://account.cengage.com/synthetic-session", "Loading", Outcome.NETWORK_ERROR),
    ],
)
async def test_webassign_wrong_page_cannot_replace_course_data(
    source_session, monkeypatch, entry, body, outcome
):
    db, context, manager = source_session
    db.update_state("webassign", authorized=True, landing_page=WEB_HOME)
    vault = Vault()
    redirect_initial_navigation(context, monkeypatch, WEB_HOME, entry)
    await context.route(
        "**/*",
        lambda route: route.fulfill(body=body, content_type="text/html"),
    )
    result = await webassign.WebAssignConnector(db, manager, vault).sync()
    assert result.outcome == outcome
    assert not result.tasks and not result.covered_task_scopes
    assert parse_qs(urlparse(vault.value["url"]).query)["UserPass"] == ["synthetic-old"]
