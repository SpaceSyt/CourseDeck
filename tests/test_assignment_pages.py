from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.connectors import assignment_pages as pagination
from coursedeck.connectors import gradescope, webassign
from coursedeck.connectors.http import TransportError
from coursedeck.db import Database
from coursedeck.domain import Outcome

NEXT = '<nav class="pagination"><a rel="next" href="?page=2">Next</a></nav>'
END = '<nav class="pagination"><span class="next disabled">Next</span></nav>'


def grade_table(*ids):
    return (
        '<table id="assignments-student-table"><tbody>'
        + "".join(
            f'<tr><th><a href="/courses/1/assignments/{key}">Task {key}</a></th>'
            "<td>Submitted</td></tr>"
            for key in ids
        )
        + "</tbody></table>"
    )


class Page:
    """In-memory navigation; unexpected URLs fail instead of reaching the network."""

    def __init__(self, url, documents):
        self.url, self.documents, self.requests = url, documents, []
        self.first = self

    async def goto(self, url, **kwargs):
        self.requests.append(url)
        assert url in self.documents
        self.url = url
        value = self.documents[url]
        return SimpleNamespace(status=value if isinstance(value, int) else 200)

    async def content(self):
        return self.documents[self.url]

    def locator(self, *args, **kwargs):
        return self

    def get_by_role(self, *args, **kwargs):
        return self

    async def wait_for(self, **kwargs):
        pass

    async def evaluate_all(self, script):
        return []

    async def count(self):
        return 0

    def on(self, *args):
        pass


@pytest.mark.parametrize(
    "control",
    [
        '<nav class="pagination"><button>Next</button></nav>',
        '<nav class="pagination"><a href="?page=3">Next</a></nav>',
        '<a rel="next" href="https://other.example/courses/1?page=2">Next</a>',
        '<a rel="next" href="/courses/2?page=2">Next</a>',
        '<a rel="next" href="?page=2&filter=open">Next</a>',
        '<nav class="pagination">1 2 3</nav>',
        '<input type="search" value="hidden">' + NEXT,
        '<div data-has-more="true"></div>',
    ],
)
async def test_unsupported_or_filtered_page_retains_rows_without_navigating(control):
    url = "https://www.gradescope.com/courses/1"
    page = Page(url, {url: grade_table(1) + control})
    pages = [
        item async for item in pagination.assignment_pages(page, await page.content(), AsyncMock())
    ]
    assert len(pages) == 1 and pages[0][2] and not pages[0][1]
    assert "Task 1" in pages[0][0] and not page.requests


@pytest.mark.parametrize(
    "ending, reason",
    [
        (grade_table(1) + END, "repeated"),
        (grade_table(2), "terminal"),
        (503, "unavailable"),
        (grade_table(2) + NEXT.replace("page=2", "page=1"), "page order"),
    ],
)
async def test_failed_continuation_retains_first_page_and_never_covers(ending, reason):
    url = "https://www.gradescope.com/courses/1"
    page = Page(url, {url: grade_table(1) + NEXT, url + "?page=2": ending})
    pages = [
        item async for item in pagination.assignment_pages(page, await page.content(), AsyncMock())
    ]
    assert "Task 1" in pages[0][0]
    assert all(not complete for _, complete, _ in pages)
    assert reason in pages[-1][2]


async def test_page_budget_and_nonfirst_start_are_explicit(monkeypatch):
    url = "https://www.gradescope.com/courses/1"
    monkeypatch.setattr(pagination, "MAX_ASSIGNMENT_PAGES", 1)
    page = Page(url, {url: grade_table(1) + NEXT})
    pages = [
        item async for item in pagination.assignment_pages(page, await page.content(), AsyncMock())
    ]
    assert "limit" in pages[-1][2] and not page.requests
    page.url += "?page=2"
    pages = [item async for item in pagination.assignment_pages(page, grade_table(2), AsyncMock())]
    assert "first page" in pages[-1][2] and not pages[-1][1]


class Browser:
    interactive = None

    def __init__(self, page):
        self.page = page

    def exists(self):
        return True

    def has_session_snapshot(self):
        return True

    @asynccontextmanager
    async def session(self, timezone=None):
        yield SimpleNamespace(new_page=AsyncMock(return_value=self.page))


@pytest.mark.parametrize("provider", ["gradescope", "webassign"])
@pytest.mark.parametrize("second_page_failure", [False, True])
async def test_sync_collects_all_pages_deduplicates_and_retains_on_failure(
    tmp_path, monkeypatch, provider, second_page_failure
):
    db = Database(tmp_path / "db")
    if provider == "gradescope":
        url = "https://www.gradescope.com/courses/1"
        second_url = url + "?page=2"
        documents = {
            "https://www.gradescope.com/account": '<a href="/courses/1">Course</a>',
            url: grade_table(1) + NEXT,
            second_url: grade_table(1, 2) + END,
        }
        db.update_state(provider, authorized=True)
        page = Page(url, documents)
        connector = gradescope.GradescopeConnector(db, Browser(page))
        monkeypatch.setattr(gradescope, "resume_session", AsyncMock(return_value=True))
    else:
        url = "https://www.webassign.net/student?class=1&UserPass=fixture"
        second_url = url + "&page=2"

        def table(*ids):
            rows = "".join(
                f'<tr><th><a href="/web/Student/Assignment-Responses/last?dep={key}">'
                f"Task {key}</a><span> (Homework)</span></th><td></td>"
                "<td>Friday, September 18, 2026 at 11:59 PM EDT</td><td>- / 10</td></tr>"
                for key in ids
            )
            return (
                '<main id="js-student-myAssignmentsPage"><a id="backNavBtn">Course</a>'
                '<button role="tab" aria-label="Show All Assignments (selected)"></button>'
                "<table><thead><tr><th>Current Assignments</th><th>Restrictions</th>"
                "<th>Due Date</th><th>Score</th></tr></thead><tbody>"
                + rows
                + "</tbody></table><p>There are no past assignments.</p></main>"
            )

        documents = {
            url: table(1) + NEXT.replace("?page=2", "?class=1&amp;UserPass=fixture&amp;page=2"),
            second_url: table(1, 2) + END,
        }
        db.update_state(provider, authorized=True, landing_page=url.split("&UserPass")[0])
        page = Page(url, documents)
        vault = SimpleNamespace(get=lambda key: {"url": url})
        connector = webassign.WebAssignConnector(db, Browser(page), vault)
        monkeypatch.setattr(webassign, "resume_session", AsyncMock(return_value=True))
        monkeypatch.setattr(webassign, "wait_for_assignment_list", AsyncMock())
        monkeypatch.setattr(webassign, "wait_for_rendered_assignments", AsyncMock())
        monkeypatch.setattr(connector, "_save_session_page", AsyncMock())
    if second_page_failure:
        documents[second_url] = 503
    result = await connector.sync()
    assert [task.external_id for task in result.tasks] == (
        ["1"] if second_page_failure else ["1", "2"]
    )
    assert len(result.covered_task_scopes) == (0 if second_page_failure else 1)
    assert second_url in page.requests
    if second_page_failure:
        assert result.outcome == Outcome.PARTIAL and any(
            "unavailable" in w for w in result.warnings
        )
    elif provider == "gradescope":
        assert result.outcome == Outcome.SUCCESS and not result.warnings


@pytest.mark.parametrize(
    "outcome",
    [Outcome.AUTH_REQUIRED, Outcome.NETWORK_ERROR, Outcome.RATE_LIMITED, Outcome.PARSE_ERROR],
)
async def test_gradescope_login_validation_only_converts_auth_errors(
    tmp_path, monkeypatch, outcome
):
    connector = gradescope.GradescopeConnector(Database(tmp_path / "db"), None)
    monkeypatch.setattr(
        gradescope, "session_html", AsyncMock(side_effect=TransportError(outcome, "safe"))
    )
    if outcome == Outcome.AUTH_REQUIRED:
        assert not await connector.validate_session(None)
    else:
        with pytest.raises(TransportError) as error:
            await connector.validate_session(None)
        assert error.value.outcome == outcome
