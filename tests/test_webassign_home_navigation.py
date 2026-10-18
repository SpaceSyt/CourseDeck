import json
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from coursedeck.connectors.webassign import WebAssignConnector
from coursedeck.db import Database
from coursedeck.domain import Outcome


async def test_home_with_current_and_past_buttons_reads_all_assignments(tmp_path):
    def table(category, identity, status):
        return (
            f"<table><thead><tr><th>{category} Assignments</th><th>Due Date</th>"
            "<th>Status</th></tr></thead><tbody><tr><th>"
            f'<a href="/assignment?dep={identity}">Assignment {identity}</a></th>'
            f"<td>2026-09-20T03:59:00Z</td><td>{status}</td></tr></tbody></table>"
        )

    current = table("Current", "1", "Incomplete")
    past = table("Past", "2", "Submitted")
    selected = '<button role="tab" aria-label="Show All Assignments (selected)">All</button>'
    all_content = current + past + selected
    list_content = (
        '<main id="js-student-myAssignmentsPage"><h1>My Assignments</h1>'
        + current
        + '<button role="tab" aria-label="Show All Assignments" onclick="showAll()">All</button>'
        + "</main>"
    )
    home = (
        '<section id="js-student-myAssignmentsWrapper">'
        '<button aria-label="Show Current Assignments (1)" '
        'onclick="openList()">Current Assignments (1)</button>'
        '<button aria-label="Show Past Assignments (1)" '
        "onclick=\"throw new Error('wrong navigation')\">Past Assignments (1)</button>"
        "</section><script>"
        f"function openList(){{document.body.innerHTML={json.dumps(list_content)};}}"
        "function showAll(){document.querySelector('#js-student-myAssignmentsPage').innerHTML="
        + json.dumps(all_content)
        + ";}</script>"
    )
    db = Database(tmp_path / "db")
    db.update_state(
        "webassign", authorized=True, landing_page="https://www.webassign.net/student?class=1"
    )

    class Vault:
        def get(self, key):
            return {"url": "https://www.webassign.net/student?UserPass=fixture"}

        def set(self, key, value):
            pass

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        requests, errors = [], []

        class Manager:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                context = await browser.new_context()

                async def respond(route):
                    requests.append(route.request.url)
                    await route.fulfill(body=home, content_type="text/html")

                context.on(
                    "page", lambda page: page.on("pageerror", lambda error: errors.append(error))
                )
                await context.route("**/*", respond)
                try:
                    yield context
                finally:
                    await context.close()

        try:
            result = await WebAssignConnector(db, Manager(), Vault()).sync()
            assert result.outcome == Outcome.SUCCESS
            assert {task.external_id for task in result.tasks} == {"1", "2"}
            assert len(result.covered_task_scopes) == 1
            assert len(requests) == 1
            assert not errors
        finally:
            await browser.close()
