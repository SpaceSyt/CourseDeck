import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as BrowserError

from coursedeck.connectors.classroom_browser import (
    DETAIL_JS,
    ORIGIN,
    ClassroomBrowserConnector,
    apply_assignment_details,
    parse_classwork_items,
    parse_courses,
    parse_task_links,
    source_id,
)
from coursedeck.connectors.classroom_connection import ClassroomConnector
from coursedeck.connectors.classroom_webdata import assignment_timing
from coursedeck.connectors.http import TransportError
from coursedeck.db import Database
from coursedeck.domain import Outcome, SyncResult, now


def encoded(value):
    return base64.b64encode(value.encode()).decode().rstrip("=")


def test_browser_ids_match_api_and_unrecognized_links_are_rejected():
    course_path = f"/u/0/c/{encoded('123')}"
    task_path = f"{course_path}/a/{encoded('456')}/details"
    links = [
        {"href": ORIGIN + course_path, "text": "Calculus\nSection A"},
        {"href": ORIGIN + task_path, "text": "Homework"},
        {"href": ORIGIN + course_path, "text": "Duplicate"},
        {"href": "https://evil.test" + course_path, "text": "Not Classroom"},
        {"href": ORIGIN + "/c/not_a_valid_id", "text": "Bad ID"},
    ]
    courses = parse_courses(links)
    tasks = parse_task_links(links, "123")
    assert len(courses) == 1 and courses[0].id == "google_classroom:123"
    assert courses[0].name == "Calculus"
    assert len(tasks) == 1 and tasks[0].id == "google_classroom:123:456"
    assert tasks[0].due_at is None and "due_at" in tasks[0].raw_data["unavailable_fields"]
    assert tasks[0].submission_status == "unknown"
    assert parse_task_links(links, "999") == []
    assert parse_courses([]) == []
    with pytest.raises(ValueError):
        source_id("arbitrary")


def test_course_card_beats_sidebar_initial_and_materials_are_not_tasks():
    url = f"{ORIGIN}/u/0/c/{encoded('123')}"
    course = parse_courses(
        [
            {"href": url, "text": "C\nCalculus\n101", "label": "Calculus 101"},
            {"href": url, "text": "", "course_heading": "Calculus\n101"},
        ]
    )[0]
    assert course.name == "Calculus" and course.section == "101"
    tasks = parse_classwork_items(
        [
            {"id": "456", "kind": "Assignment", "title": "Homework"},
            {"id": "789", "kind": "Material", "title": "Syllabus"},
            {"id": "not-an-id", "kind": "Assignment", "title": "Invalid"},
        ],
        course,
    )
    assert len(tasks) == 1 and tasks[0].external_id == "456"


def test_page_response_dates_require_matching_identity_title_and_schema():
    header = [
        ["456", ["123"]],
        1788274776151,
        1788457142327,
        None,
        None,
        "Homework",
        None,
        None,
        2,
        None,
        None,
    ]
    record = [2, [header, [1788982200000, 1788382819730, True], "unrelated-private-field"]]

    def envelope(value):
        return ")]}'\n1234\n" + json.dumps([["wrb.fr", "synthetic", json.dumps([value])]])

    result = assignment_timing(envelope(record), "123", "456", "Homework")
    assert result["due_at"].isoformat() == "2026-09-09T19:30:00+00:00"
    assert set(result) == {"due_at", "source_updated_at"}
    assert assignment_timing(envelope(record), "wrong", "456", "Homework") is None
    assert assignment_timing(envelope(record), "123", "456", "Wrong title") is None
    assert assignment_timing("not-json", "123", "456", "Homework") is None
    record[1][1][0] = None
    assert assignment_timing(envelope(record), "123", "456", "Homework") is None
    record[1][1][0] = "Sep 9, 3:30 PM"
    assert assignment_timing(envelope(record), "123", "456", "Homework") is None
    record[1][1][0] = None
    record[1][1][2] = False
    explicit_none = assignment_timing(envelope(record), "123", "456", "Homework")
    assert explicit_none is not None and explicit_none["due_at"] is None
    assert explicit_none["source_updated_at"] is not None
    record[1][1][2] = None
    assert assignment_timing(envelope(record), "123", "456", "Homework") is None


def test_completed_classwork_and_late_submission_are_recognized():
    course = parse_courses([{"href": f"{ORIGIN}/c/{encoded('123')}", "text": "Writing"}])[0]
    tasks = parse_classwork_items(
        [
            {"id": "456", "kind": "Completed Assignment", "title": "Essay"},
            {"id": "789", "kind": "Question", "title": "Unsupported question"},
        ],
        course,
    )
    assert len(tasks) == 1 and tasks[0].submission_status == "completed"
    assert "submission_status" not in tasks[0].raw_data["unavailable_fields"]
    apply_assignment_details(tasks[0], {"statuses": ["Turned in late", "Turned in late"]})
    assert tasks[0].submission_status == "submitted"


def test_explicit_no_deadline_can_clear_previous_deadline_but_missing_flag_cannot(tmp_path):
    db = Database(tmp_path / "db")
    course = parse_courses([{"href": f"{ORIGIN}/c/{encoded('123')}", "text": "Writing"}])[0]
    known = browser_task()
    known.due_at = now()
    known.raw_data["unavailable_fields"].remove("due_at")
    db.apply(
        "google_classroom",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[known]),
        now().isoformat(),
    )
    unknown = browser_task()
    db.apply(
        "google_classroom",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[unknown]),
        now().isoformat(),
    )
    assert db.tasks()[0]["due_at"] is not None
    unknown.raw_data["unavailable_fields"].remove("due_at")
    db.apply(
        "google_classroom",
        SyncResult(outcome=Outcome.PARTIAL, courses=[course], tasks=[unknown]),
        now().isoformat(),
    )
    assert db.tasks()[0]["due_at"] is None


async def test_default_browser_needs_no_vault_and_switch_is_explicit(tmp_path):
    db = Database(tmp_path / "db")
    vault = Mock()
    vault.get.side_effect = AssertionError("Browser flow must not use vault")
    vault.delete.side_effect = AssertionError("Browser flow must not use vault")
    manager = Mock(interactive=None, login=AsyncMock(), close=AsyncMock(), reset=AsyncMock())
    connector = ClassroomConnector(db, vault, manager)
    assert connector.mode == "browser" and connector.manual_login
    assert connector.connection_status() == "not_connected"
    assert "client_id" not in [f["key"] for f in connector.configuration_fields]
    await connector.connect()
    manager.login.assert_awaited_once()
    assert manager.login.call_args.args[0] == ORIGIN + "/u/0/h"
    await connector.disconnect()
    manager.reset.assert_awaited_once()
    vault.get.assert_not_called()
    vault.delete.assert_not_called()
    await connector.configure({"transport": "official_api", "timezone": "America/New_York"})
    assert connector.mode == "official_api" and not connector.manual_login
    assert connector.connection_status() == "not_configured"
    assert "client_id" in [f["key"] for f in connector.configuration_fields]
    with pytest.raises(ValueError):
        await connector.configure({"transport": "invented"})
    await connector.configure({"transport": "browser"})
    assert connector.connection_status() == "not_connected"
    with pytest.raises(ValueError):
        await connector.configure({"base_url": "https://evil.test"})


def test_existing_api_users_keep_connection_method(tmp_path):
    db = Database(tmp_path / "db")
    db.update_state("google_classroom", configured=True, authorized=True)
    connector = ClassroomConnector(db, Mock(), Mock())
    assert connector.mode == "official_api"
    assert connector.connection_status() == "connected"


def test_incomplete_browser_coverage_does_not_remove_known_deadline_or_note(tmp_path):
    db = Database(tmp_path / "db")
    links = [
        {"href": f"{ORIGIN}/c/{encoded('123')}", "text": "Course"},
        {"href": f"{ORIGIN}/c/{encoded('123')}/a/{encoded('456')}/details", "text": "Task"},
    ]
    courses, tasks = parse_courses(links), parse_task_links(links, "123")
    known = tasks[0].model_copy(deep=True)
    known.due_at = now()
    known.raw_data = {}
    known.submission_status = "submitted"
    known.score, known.graded = 0, True
    db.apply(
        "google_classroom",
        SyncResult(outcome=Outcome.SUCCESS, courses=courses, tasks=[known]),
        now().isoformat(),
    )
    db.patch_local(known.id, {"note": "Do not lose this"})
    db.apply(
        "google_classroom",
        SyncResult(outcome=Outcome.PARTIAL, courses=courses, tasks=tasks),
        now().isoformat(),
    )
    for _ in range(4):
        db.apply("google_classroom", SyncResult(outcome=Outcome.PARTIAL), now().isoformat())
    assert datetime.fromisoformat(db.tasks()[0]["due_at"]) == known.due_at
    assert db.tasks()[0]["local"]["note"] == "Do not lose this"
    assert db.tasks()[0]["submission_status"] == "submitted"
    assert db.tasks()[0]["score"] == 0 and db.tasks()[0]["graded"]
    assert not db.tasks()[0]["archived"] and db.tasks()[0]["missing_count"] == 0


def browser_task():
    return parse_task_links(
        [{"href": f"{ORIGIN}/c/{encoded('123')}/a/{encoded('456')}/details", "text": "Essay"}],
        "123",
    )[0]


def test_visible_description_and_submission_status_are_saved_without_inventing_grade():
    task = browser_task()
    apply_assignment_details(
        task,
        {
            "description": "Part one\n\nPart two: updated instructions",
            "statuses": ["Assigned", "Assigned"],
            "grade_labels": ["Due 3:30 PM"],
        },
    )
    assert task.description == "Part one\n\nPart two: updated instructions"
    assert task.submission_status == "open"
    assert "description" not in task.raw_data["unavailable_fields"]
    assert "submission_status" not in task.raw_data["unavailable_fields"]
    assert {"score", "graded", "points_possible"} <= set(task.raw_data["unavailable_fields"])


@pytest.mark.parametrize(
    "label, score, points, graded",
    [
        ("0 / 100 points", 0, 100, True),
        ("8.5/10", 8.5, 10, True),
        ("Ungraded", None, None, False),
    ],
)
def test_explicit_grade_zero_and_ungraded_labels(label, score, points, graded):
    task = browser_task()
    apply_assignment_details(task, {"grade_labels": [label], "statuses": ["Returned"]})
    assert (task.score, task.points_possible, task.graded) == (score, points, graded)
    assert task.submission_status == "returned"
    assert not {"score", "points_possible", "graded"} & set(task.raw_data["unavailable_fields"])


def test_points_only_is_not_a_grade_and_conflicting_status_does_not_complete_task():
    task = browser_task()
    apply_assignment_details(
        task, {"grade_labels": ["20 points"], "statuses": ["Assigned", "Turned in"]}
    )
    assert task.points_possible == 20
    assert task.submission_status == "unknown" and task.score is None
    assert {"score", "graded", "submission_status", "description"} <= set(
        task.raw_data["unavailable_fields"]
    )
    other = browser_task()
    apply_assignment_details(other, {"grade_labels": ["5 / 10", "10 / 10"]})
    assert other.score is None and "score" in other.raw_data["unavailable_fields"]


async def test_description_dom_is_sibling_of_assignment_header_and_excludes_comments():
    # Exercise the observed DOM structure with synthetic content, including a
    # different assignment and a private comment that must never become body.
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content("""<div>
              <div data-stream-item-id="456"><h1>Essay</h1>
                <div class="W4hhKd">0 / 20 points</div></div>
              <div class="nGi02b"><p>Read the chapter.</p><p>Updated requirement.</p></div>
              <div>Private comments: private text</div>
            </div><div><div data-stream-item-id="999"><h1>Other assignment</h1></div>
              <div class="nGi02b">Other body</div></div>
            <div data-submission-id="synthetic"><span class="u7S8tc">Turned in</span></div>""")
            detail = await page.evaluate(DETAIL_JS, "456")
            assert "Read the chapter." in detail["description"]
            assert "Updated requirement." in detail["description"]
            assert "private text" not in detail["description"]
            assert "Other body" not in detail["description"]
            task = browser_task()
            apply_assignment_details(task, detail)
            assert task.submission_status == "submitted"
            assert task.score == 0 and task.points_possible == 20
        finally:
            await browser.close()


async def test_empty_archive_does_not_import_active_courses_from_sidebar(tmp_path):
    from playwright.async_api import async_playwright

    connector = ClassroomBrowserConnector(Database(tmp_path / "db"), Mock())
    connector.read_page = AsyncMock()
    connector.expand_list = AsyncMock()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(f'''<a href="{ORIGIN}/c/{encoded("123")}">Active course</a>
                <main>None of your classes have been archived</main>''')
            assert (
                await connector.read_course_list(page, ORIGIN + "/u/0/h/archived", archived=True)
                == []
            )
            await page.set_content(f'''<a href="{ORIGIN}/c/{encoded("123")}">Active course</a>
                <li data-course-id="789"><h2>Archived calculus</h2>
                <a href="{ORIGIN}/c/{encoded("789")}">Archived calculus</a></li>''')
            courses = await connector.read_course_list(
                page, ORIGIN + "/u/0/h/archived", archived=True
            )
            assert len(courses) == 1 and courses[0].external_id == "789"
            assert courses[0].raw_metadata["archived"] is True
        finally:
            await browser.close()


@pytest.mark.parametrize(
    "failure",
    [
        TransportError(Outcome.NETWORK_ERROR, "Temporary source failure"),
        BrowserError("Browser failure"),
    ],
)
async def test_failed_archive_and_course_do_not_skip_remaining_courses(tmp_path, failure):
    courses = parse_courses(
        [
            {"href": f"{ORIGIN}/c/{encoded(cid)}", "text": title}
            for cid, title in [("123", "Unavailable course"), ("789", "Readable course")]
        ]
    )
    page = Mock()

    @asynccontextmanager
    async def session(_timezone):
        yield Mock(new_page=AsyncMock(return_value=page))

    manager = Mock(session=session)
    connector = ClassroomBrowserConnector(Database(tmp_path / "db"), manager)
    connector.connection_status = Mock(return_value="connected")
    connector.read_course_list = AsyncMock(side_effect=[courses, failure])

    async def read_course(_page, course, result):
        if course.external_id == "123":
            raise failure
        task = browser_task()
        task.course_external_id = "789"
        result.tasks.append(task)

    connector.read_course = AsyncMock(side_effect=read_course)
    result = await connector.sync()
    assert result.outcome == Outcome.PARTIAL
    assert [task.course_external_id for task in result.tasks] == ["789"]
    assert any("Archived Classroom" in warning for warning in result.warnings)
    assert any("Unavailable course" in warning for warning in result.warnings)
    assert "score" in result.metadata["unavailable_assignment_fields"]
    assert "points_possible" not in " ".join(result.warnings)
    assert "grade and points" in " ".join(result.warnings)


@pytest.mark.parametrize("response_first", [False, True])
async def test_assignment_waits_for_canonical_heading_and_timing_response(tmp_path, response_first):
    connector = ClassroomBrowserConnector(Database(tmp_path / "db"), Mock())
    task = browser_task()
    task.title = "3:30 PM – Essay"
    callbacks = {}
    page = Mock()
    page.on.side_effect = lambda name, callback: callbacks.update({name: callback})
    page.remove_listener.side_effect = lambda name, _callback: callbacks.pop(name, None)
    page.locator.return_value.inner_text = AsyncMock(return_value="Essay")
    page.evaluate = AsyncMock(
        return_value={"description": "Full instructions", "statuses": ["Assigned"]}
    )
    header = [["456", ["123"]], None, 1788457142327, None, None, "Essay", None, None, 2, None, None]
    body = json.dumps(
        [["wrb.fr", "synthetic", json.dumps([[2, [header, [1788982200000, None, True]]]])]]
    )
    response = Mock(
        url=ORIGIN + "/synthetic/batchexecute", status=200, text=AsyncMock(return_value=body)
    )

    async def rendered(_page, _url):
        # No response is pending when read_page returns. It arrives after the
        # heading settles, as can happen with Classroom's delayed page requests.
        if response_first:
            callbacks["response"](response)
            await asyncio.sleep(0.02)
        else:
            asyncio.get_running_loop().call_later(
                0.02, lambda: callbacks.get("response", lambda _response: None)(response)
            )

    connector.read_page = AsyncMock(side_effect=rendered)
    await connector.read_assignment(page, task)
    assert task.due_at.isoformat() == "2026-09-09T19:30:00+00:00"
    assert task.title == "Essay"
    assert "due_at" not in task.raw_data["unavailable_fields"]
    assert callbacks == {}
