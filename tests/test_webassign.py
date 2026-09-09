import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import async_playwright

from coursedeck.connectors.webassign import WebAssignConnector, parse_webassign, safe_page_url
from coursedeck.db import Database
from coursedeck.domain import Outcome


def test_experimental_parser_preserves_unknown_submission():
    html = (Path(__file__).parent / "fixtures/webassign/assignments.html").read_text()
    course, tasks, warnings = parse_webassign(
        html, "https://www.webassign.net/student?class=123", None
    )
    assert course.external_id == "123" and tasks[0].external_id == "456"
    assert tasks[0].due_at.isoformat() == "2026-09-06T03:59:00+00:00"
    assert tasks[0].submission_status == "unknown"
    assert not warnings


def test_no_untrusted_or_secret_urls():
    assert safe_page_url("https://www.webassign.net/student?class=123&token=secret").endswith(
        "?class=123"
    )
    with pytest.raises(ValueError):
        safe_page_url("https://webassign.net.evil.test/student")


def test_unrelated_timestamp_is_never_used_as_a_deadline():
    html = '<h1>Calculus</h1><table><tr><td><a href="/assignment?dep=1">Work</a>'
    html += '<time datetime="2026-01-01T00:00:00Z">Available from</time></td></tr></table>'
    _, tasks, warnings = parse_webassign(html, "https://www.webassign.net/student?class=1", None)
    assert tasks[0].due_at is None and warnings


ASSIGNMENTS = """<main id="js-student-myAssignmentsPage">
<a id="backNavBtn">Calculus fixture</a><h1>My Assignments</h1>
<table><thead><tr><th>Current Assignments</th><th>Restrictions</th>
<th>Due Date</th><th>Score</th></tr></thead><tbody><tr>
<th><a href="/web/Student/Assignment-Responses/last?dep=456&UserPass=fixture-secret">
Integration</a><span> (Homework)</span></th><td></td>
<td>Friday, September 18, 2026 at 11:59 PM EDT</td><td>- / 10</td>
</tr></tbody></table></main>"""


def test_current_student_markup_uses_source_deadline_and_identity():
    course, tasks, warnings = parse_webassign(
        ASSIGNMENTS,
        "https://www.webassign.net/v4cgi/student.pl?action=home/index&course=123,456&UserPass=fixture-secret",
        None,
    )
    assert course.external_id == "123,456" and course.name == "Calculus fixture"
    assert tasks[0].title == "Integration"
    assert tasks[0].due_at.isoformat() == "2026-09-19T03:59:00+00:00"
    assert tasks[0].submission_status == "unknown"
    assert "UserPass" not in tasks[0].url and "UserPass" not in course.source_url
    assert not warnings


async def test_login_survives_fresh_context_with_vault_cookies(tmp_path):
    db = Database(tmp_path / "db")

    class Vault:
        records = {}

        def get(self, name):
            return self.records.get(name)

        def set(self, name, value):
            self.records[name] = value

    vault = Vault()
    requests = []
    assignment_view = ASSIGNMENTS + (
        '<button role="tab" aria-label="Show All Assignments" onclick="showAll(this)">'
        "All Assignments</button>"
    )
    past_view = ASSIGNMENTS.replace("dep=456", "dep=789").replace("Integration", "Review")
    home = '<div id="js-student-myAssignmentsWrapper"><h2>My Assignments</h2>'
    home += '<button onclick="showAssignments()">Current Assignments</button></div>'
    home += (
        "<script>function showAssignments(){document.body.innerHTML="
        + json.dumps(assignment_view)
        + ";}"
    )
    home += "function showAll(button){"
    home += 'button.setAttribute("aria-label","Show All Assignments (selected)");'
    home += 'document.body.insertAdjacentHTML("beforeend",' + json.dumps(past_view) + ");}</script>"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def route_page(route):
            requests.append((route.request.url, await route.request.header_value("cookie")))
            await route.fulfill(body=home, content_type="text/html")

        class Manager:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                context = await browser.new_context()
                assert not await context.cookies()
                await context.route("https://www.webassign.net/**", route_page)
                try:
                    yield context
                finally:
                    await context.close()

        connector = WebAssignConnector(db, Manager(), vault)
        login = await browser.new_context()
        await login.route("https://www.webassign.net/**", route_page)
        await login.add_cookies(
            [
                {
                    "name": "session",
                    "value": "fixture-cookie",
                    "domain": "www.webassign.net",
                    "path": "/",
                }
            ]
        )
        page = await login.new_page()
        await page.goto(
            "https://www.webassign.net/v4cgi/student.pl?action=home/index&course=123,456&UserPass=fixture-secret"
        )
        assert await connector.validate_session(login)
        await login.close()
        db.update_state("webassign", authorized=True)
        for _ in range(2):
            result = await connector.sync()
            assert result.outcome == Outcome.PARTIAL and len(result.tasks) == 2
            assert {task.external_id for task in result.tasks} == {"456", "789"}
            assert result.tasks[0].due_at is not None
            assert parse_qs(urlparse(requests[-1][0]).query)["UserPass"] == ["fixture-secret"]
            assert "session=fixture-cookie" in requests[-1][1]
        assert "fixture-secret" not in str(db.state("webassign"))
        await browser.close()
