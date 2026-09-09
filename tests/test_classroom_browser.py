import base64
import json
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from coursedeck.connectors.classroom_browser import (
    ORIGIN,
    parse_classwork_items,
    parse_courses,
    parse_task_links,
    source_id,
)
from coursedeck.connectors.classroom_connection import ClassroomConnector
from coursedeck.connectors.classroom_webdata import assignment_timing
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
