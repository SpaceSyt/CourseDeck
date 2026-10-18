import pytest

from coursedeck.connectors.gradescope import assignment_list_complete, parse_assignments
from coursedeck.domain import Course


def assignment_page(status):
    return (
        '<table id="assignments-student-table"><tbody><tr><th>'
        '<a href="/courses/1/assignments/2">Exercise</a></th>'
        f"<td>{status}</td><td></td></tr></tbody></table>"
    )


@pytest.mark.parametrize(
    ("label", "status"),
    [
        ("Submitted", "submitted"),
        ("Submitted (Late)", "submitted"),
        ("Graded", "graded"),
        ("0 / 10", "graded"),
        ("No Submission", "open"),
        ("Not submitted", "open"),
        ("Awaiting submission", "open"),
        ("Submission missing", "missing"),
        ("Submission processing", "unknown"),
        ("Submission failed", "unknown"),
        ("Submission reopened", "unknown"),
        ("Submission", "unknown"),
    ],
)
def test_submission_labels_require_positive_completion_evidence(label, status):
    course = Course(
        provider="gradescope",
        external_id="1",
        name="Course",
        source_url="https://www.gradescope.com/courses/1",
    )
    tasks, _ = parse_assignments(assignment_page(label), course)
    assert tasks[0].submission_status == status
    assert tasks[0].graded == (status == "graded")


def test_reconciliation_requires_known_unfiltered_complete_list():
    html = assignment_page("Submitted")
    assert assignment_list_complete(html, [])
    assert not assignment_list_complete(html, ["Skipped row"])
    assert not assignment_list_complete(html.replace("assignments-student-table", "other"), [])
    assert not assignment_list_complete(html + '<nav class="pagination">Next</nav>', [])
    assert not assignment_list_complete(html + '<input type="search" value="exercise">', [])
    assert assignment_list_complete(
        '<table id="assignments-student-table"><tbody></tbody></table>', []
    )


def test_unidentified_dated_row_preserves_visible_evidence_in_warning():
    course = Course(provider="gradescope", external_id="1", name="Physics")
    html = (
        '<table id="assignments-student-table"><tbody><tr><th>Written lab</th>'
        '<td>No Submission</td><td><time datetime="2026-10-01T23:59:00-04:00">'
        "Oct 1</time></td></tr></tbody></table>"
    )
    tasks, warnings = parse_assignments(html, course)
    assert tasks == []
    assert len(warnings) == 1
    for evidence in ("Physics", "Written lab", "No Submission", "2026-10-01T23:59:00-04:00"):
        assert evidence in warnings[0]
    assert not assignment_list_complete(html, warnings)
