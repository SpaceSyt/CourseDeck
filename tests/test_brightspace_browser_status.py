from types import SimpleNamespace

import pytest

from coursedeck.connectors.brightspace import BrightspaceAPITransport
from coursedeck.connectors.brightspace_browser_status import (
    BrightspaceBrowserStatus,
    folder_statuses,
)
from coursedeck.connectors.http import TransportError
from coursedeck.domain import Course


def course():
    return Course(
        provider="brightspace",
        external_id="1",
        name="Fixture",
        source_url="https://school.example/d2l/home/1",
    )


def row(name="Reading", status="Not Submitted", href=None):
    title = f'<a href="{href}">{name}</a>' if href else f"<label>{name}</label>"
    return f'<tr><th><div class="d2l-foldername">{title}</div></th><td>{status}</td></tr>'


def test_restricted_folder_status_uses_unique_existing_identity():
    folders = [{"Id": 2, "Name": "Reading"}]
    assert (
        folder_statuses("<table>" + row() + "</table>", course(), folders)["2"]["status"] == "open"
    )
    assert not folder_statuses("<table>" + row() + row() + "</table>", course(), folders)
    assert not folder_statuses(
        "<table>" + row() + "</table>", course(), folders + [{"Id": 3, "Name": "Reading"}]
    )


def test_linked_status_checks_identity_and_conflict():
    folders = [{"Id": 2, "Name": "Reading"}]
    good = row(status="Completed", href="/d2l/lms/dropbox/user/folder_submit_files.d2l?db=2&ou=1")
    assert (
        folder_statuses("<table>" + good + "</table>", course(), folders)["2"]["status"]
        == "completed"
    )
    assert not folder_statuses(
        "<table>" + good.replace("ou=1", "ou=9") + "</table>", course(), folders
    )
    bad = good.replace("Completed", "Not Submitted")
    assert (
        folder_statuses("<table>" + good + bad + "</table>", course(), folders)["2"]["status"]
        == "unknown"
    )
    assert not folder_statuses(
        '<div class="d2l-htmlblock-untrusted"><table>' + good + "</table></div>", course(), folders
    )


def test_wrong_course_page_is_never_used_for_title_fallback():
    reader = BrightspaceBrowserStatus(None, "https://school.example")
    with pytest.raises(TransportError):
        reader.check_page(
            SimpleNamespace(
                url="https://school.example/d2l/lms/dropbox/user/folders_list.d2l?ou=9"
            ),
            "/d2l/lms/dropbox/user/folders_list.d2l",
            "1",
        )


async def test_dom_status_resolves_api_permission_gap_without_erasing_grade():
    async def get(path, params):
        if "whoami" in path:
            return {"Identifier": "99"}
        if "myenrollments" in path:
            return {
                "Items": [{"OrgUnit": {"Id": 1, "Name": "Fixture"}}],
                "PagingInfo": {"HasMoreItems": False},
            }
        if "mysubmissions" in path:
            raise TransportError("partial", "Permission denied")
        if "dropbox" in path:
            return [{"Id": 2, "Name": "Reading", "DueDate": None}]
        if "discussions" in path:
            return []
        return {"Objects": [], "Next": None}

    async def statuses(course, folders):
        return {"2": {"status": "completed", "evidence": "student_assignment_list"}}

    result = await BrightspaceAPITransport(
        get,
        "https://school.example",
        "1.49",
        "1.82",
        browser_status=SimpleNamespace(folders=statuses, quiz=None),
    ).sync()
    task = result.tasks[0]
    assert task.submission_status == "completed"
    assert "submission_status" not in task.raw_data["unavailable_fields"]
    assert "score" in task.raw_data["unavailable_fields"]
    assert not any("Permission denied" in warning for warning in result.warnings)
