import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from coursedeck.connectors.http import TransportError
from coursedeck.connectors.webassign import (
    WebAssignConnector,
    assignment_list_complete,
    homework_page_matches,
    parse_homework_submission,
    parse_webassign,
    safe_page_url,
    wait_for_assignment_list,
)
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

PAST_ASSIGNMENTS = (
    ASSIGNMENTS[ASSIGNMENTS.index("<table>") : ASSIGNMENTS.index("</main>")]
    .replace("Current Assignments", "Past Assignments")
    .replace("dep=456", "dep=789")
)


def homework_details(counts):
    html = '<div id="js-assignment-wrapper">'
    for number in range(1, len(counts) + 1):
        html += f'<a aria-label="Question {number} of {len(counts)},">{number}</a>'
    html += "</div>"
    for number, question in enumerate(counts, 1):
        html += f'<div class="waQBox"><h2 aria-label="Question {number}"></h2>'
        html += '<div class="questionPartDetails"><div class="columnLeft"><table><tbody>'
        html += "<tr><td>Question Part</td></tr><tr><td>Points</td></tr>"
        html += "<tr><td>Submissions Used</td></tr></tbody></table></div>"
        html += '<div class="columnContent"><table><tbody><tr>'
        html += "".join(f"<td>{part}</td>" for part in range(1, len(question) + 1))
        html += "</tr><tr>" + "<td>– / 1</td>" * len(question) + "</tr><tr>"
        html += "".join(f'<td class="submissions">{used}/10</td>' for used in question)
        html += "</tr></tbody></table></div></div></div>"
    return html


@pytest.mark.parametrize(
    ("counts", "status", "submitted_parts"),
    [([[0], [0, 0]], "open", 0), ([[1], [0, 2]], "open", 2), ([[1], [1, 2]], "submitted", 3)],
)
def test_homework_counts_each_submitted_part_without_using_score(counts, status, submitted_parts):
    parsed, evidence = parse_homework_submission(homework_details(counts))
    assert parsed == status
    assert evidence == {
        "kind": "question_part_submissions",
        "questions": 2,
        "parts": 3,
        "submitted_parts": submitted_parts,
    }


@pytest.mark.parametrize(
    ("old", "replacement"),
    [
        ("Question 2 of 2,", "Question 2 of 3,"),
        ('Question 2"', 'Question 1"'),
        ("<td>2</td>", "<td>1</td>"),
        ("Submissions Used", "Submissions Remaining"),
        ("1/10", "?/10"),
    ],
)
def test_homework_incomplete_or_ambiguous_part_inventory_is_unknown(old, replacement):
    html = homework_details([[1], [1, 1]]).replace(old, replacement)
    assert parse_homework_submission(html) == ("unknown", {})


def test_homework_current_reopened_state_takes_priority_over_historical_counts():
    html = homework_details([[1], [1, 1]])
    html += '<span class="submission-status">Reopened</span>'
    assert parse_homework_submission(html)[0] == "open"
    assert parse_homework_submission("<h1>No questions</h1>") == ("unknown", {})
    html = homework_details([[1], [1, 1]])
    html += '<span data-submission-status="  NOT  Submitted  "></span>'
    assert parse_homework_submission(html)[0] == "open"


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/web/Student/Assignment-Responses/last?dep=456",
        "https://account.webassign.net/web/Student/Assignment-Responses/last?dep=456",
        "https://www.webassign.net/gradebook?dep=456",
        "http://www.webassign.net/web/Student/Assignment-Responses/last?dep=456",
        "https://www.webassign.net:8443/web/Student/Assignment-Responses/last?dep=456",
        "https://www.webassign.net/web/Student/Assignment-Responses/last?dep=999",
    ],
)
def test_homework_redirects_must_preserve_origin_path_and_assignment_identity(url):
    expected = "https://www.webassign.net/web/Student/Assignment-Responses/last?dep=456"
    assert not homework_page_matches(url, expected, "456")
    assert homework_page_matches(expected + "&UserPass=fixture", expected, "456")


@pytest.mark.parametrize("restriction", ["Timed", "<svg></svg>", '<span title="Timed"></span>'])
def test_homework_detail_reads_require_known_empty_restrictions(restriction):
    html = ASSIGNMENTS.replace("<td></td>", f'<td data-test="restrictions">{restriction}</td>')
    _, tasks, _ = parse_webassign(html, "https://www.webassign.net/student?class=123", None)
    assert not tasks[0].raw_data["detail_read_allowed"]


async def test_safe_homework_sync_reads_part_counts_without_submitting(tmp_path):
    selected = '<button role="tab" aria-label="Show All Assignments (selected)">All</button>'
    document = ASSIGNMENTS.replace("<td></td>", '<td data-test="restrictions"></td>')
    document = document.replace(
        "</main>", selected + "<p>There are no past assignments.</p></main>"
    )
    requests = []
    db = Database(tmp_path / "db")
    db.update_state(
        "webassign", authorized=True, landing_page="https://www.webassign.net/student?class=123"
    )

    class Vault:
        def get(self, key):
            return {"url": "https://www.webassign.net/student?UserPass=fixture"}

        def set(self, key, value):
            pass

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def respond(route):
            requests.append((route.request.method, urlparse(route.request.url).path))
            body = (
                homework_details([[1], [0]])
                if "Assignment-Responses" in route.request.url
                else document
            )
            await route.fulfill(body=body, content_type="text/html")

        class Manager:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                context = await browser.new_context()
                await context.route("https://www.webassign.net/**", respond)
                try:
                    yield context
                finally:
                    await context.close()

        result = await WebAssignConnector(db, Manager(), Vault()).sync()
        assert result.outcome == Outcome.SUCCESS and not result.warnings
        assert result.tasks[0].submission_status == "open"
        assert result.tasks[0].raw_data["submission_evidence"]["submitted_parts"] == 1
        assert "submission_status" not in result.tasks[0].raw_data["unavailable_fields"]
        assert len(requests) == 2 and all(method == "GET" for method, _ in requests)
        await browser.close()


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


@pytest.mark.parametrize("score", ["0 / 10", "5 / 10", "10 / 10"])
def test_score_alone_never_proves_webassign_completion(score):
    _, tasks, _ = parse_webassign(
        ASSIGNMENTS.replace("- / 10", score),
        "https://www.webassign.net/student?class=123",
        None,
    )
    assert tasks[0].submission_status == "unknown"
    assert "submission_status" in tasks[0].raw_data["unavailable_fields"]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Completed", "completed"),
        ("Submitted", "submitted"),
        ("Incomplete", "open"),
        ("In progress", "open"),
        ("Submission failed", "unknown"),
    ],
)
def test_webassign_explicit_assignment_status(label, expected):
    html = ASSIGNMENTS.replace("<th>Score</th>", "<th>Status</th>").replace("- / 10", label)
    _, tasks, _ = parse_webassign(html, "https://www.webassign.net/student?class=123", None)
    assert tasks[0].submission_status == expected


def test_webassign_coverage_requires_all_assignments_without_pagination():
    assert not assignment_list_complete(ASSIGNMENTS, [])
    selected = '<button role="tab" aria-label="Show All Assignments (selected)"></button>'
    html = ASSIGNMENTS.replace("</main>", selected + "</main>")
    assert not assignment_list_complete(html, [])
    html = html.replace("</main>", "<section>There are no past assignments.</section></main>")
    assert assignment_list_complete(html, [])
    assert not assignment_list_complete(html, ["An unreadable assignment"])
    assert not assignment_list_complete(
        html.replace("</main>", '<nav class="pagination"></nav></main>'), []
    )
    assert not assignment_list_complete(html.replace("dep=456", "unknown=456"), [])


@pytest.mark.parametrize("redirect", [True, False])
async def test_delayed_logout_is_auth_required_while_page_is_still_open(redirect):
    class Page:
        url = (
            "https://account.cengage.com/logout" if redirect else "https://www.webassign.net/logout"
        )

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        async def wait_for(self, **kwargs):
            raise BrowserError("Timed out waiting for assignments")

        async def content(self):
            return "<body>You have successfully been logged out.</body>"

        async def inner_text(self, **kwargs):
            return "You have successfully been logged out."

    with pytest.raises(TransportError) as error:
        await wait_for_assignment_list(Page(), "table")
    assert error.value.outcome == Outcome.AUTH_REQUIRED


@pytest.mark.parametrize(
    "html",
    [
        "<h1>My Assignments</h1><table><tr><td>Navigation</td></tr></table>",
        '<main id="js-student-myAssignmentsPage"><section>Loading</section></main>',
    ],
)
def test_unrecognized_empty_page_is_never_success(html):
    with pytest.raises(TransportError) as error:
        parse_webassign(html, "https://www.webassign.net/student?class=123", None)
    assert error.value.outcome == Outcome.PARSE_ERROR


async def test_layout_table_and_delayed_all_assignments_do_not_hide_tasks(tmp_path):
    current = ASSIGNMENTS.replace(
        "</main>",
        '<button role="tab" aria-label="Show All Assignments" '
        'onclick="showAll()">All</button></main>',
    )
    selected = '<button role="tab" aria-label="Show All Assignments (selected)">All</button>'
    loading = ASSIGNMENTS.replace(
        "</main>", selected + "<section><h2>Past Assignments</h2>Loading</section></main>"
    )
    all_assignments = ASSIGNMENTS.replace("</main>", selected + PAST_ASSIGNMENTS + "</main>")
    document = "<table><tr><td>Navigation layout</td></tr></table><h1>My Assignments</h1><script>"
    document += "setTimeout(()=>{document.body.innerHTML=" + json.dumps(current) + ";},120);"
    document += "function showAll(){document.body.innerHTML=" + json.dumps(loading) + ";"
    document += (
        "setTimeout(()=>{document.body.innerHTML=" + json.dumps(all_assignments) + ";},150);}"
    )
    document += "</script>"
    db = Database(tmp_path / "db")
    db.update_state(
        "webassign", authorized=True, landing_page="https://www.webassign.net/student?class=123"
    )

    class Vault:
        def get(self, key):
            return {"url": "https://www.webassign.net/student?UserPass=fixture"}

        def set(self, key, value):
            pass

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        class Manager:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                context = await browser.new_context()
                await context.route(
                    "https://www.webassign.net/**",
                    lambda route: route.fulfill(body=document, content_type="text/html"),
                )
                try:
                    yield context
                finally:
                    await context.close()

        result = await WebAssignConnector(db, Manager(), Vault()).sync()
        assert result.outcome == Outcome.PARTIAL
        assert {task.external_id for task in result.tasks} == {"456", "789"}
        assert len(result.covered_task_scopes) == 1
        await browser.close()


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
    past_view = PAST_ASSIGNMENTS.replace("Integration", "Review")
    home = '<div id="js-student-myAssignmentsWrapper"><h2>My Assignments</h2>'
    home += '<button onclick="showAssignments()">Current Assignments</button></div>'
    home += (
        "<script>function showAssignments(){document.body.innerHTML="
        + json.dumps(assignment_view)
        + ";}"
    )
    home += "function showAll(button){"
    home += 'button.setAttribute("aria-label","Show All Assignments (selected)");'
    home += (
        'document.querySelector("#js-student-myAssignmentsPage").insertAdjacentHTML("beforeend",'
        + json.dumps(past_view)
        + ");}</script>"
    )
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
