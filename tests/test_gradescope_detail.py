import json
from types import SimpleNamespace

import pytest

from coursedeck.connectors.gradescope_detail import (
    MAX_QUESTIONS,
    collect_task_detail,
    parse_detail_html,
    safe_detail_url,
    store_task_detail,
)
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now


@pytest.fixture
def task():
    return Task(
        provider="gradescope",
        course_external_id="1",
        external_id="2",
        title="Homework",
        submission_status="graded",
        graded=True,
        score=0,
        points_possible=10,
        url="https://www.gradescope.com/courses/1/assignments/2",
        raw_data={"detail_url_observed": True},
    )


HTML = """<table><tr><th>Question</th><th>Score</th></tr>
<tr><td>1: Proof</td><td>0 / 10</td></tr>
<tr><td>2: Analysis</td><td>Not released</td></tr></table>
<section><h2>Rubric</h2><p>-2: Explanation incomplete</p></section>
<section aria-label="Feedback">Please justify the induction step.</section>
<section><h2>Submitted code</h2><pre>print(1)</pre></section>
<section hidden><h2>Feedback</h2>SECRET</section>
<script>privateCredential = 'SECRET'</script>"""


def test_semantic_html_scores_and_evidence_remain_partial(task):
    result = parse_detail_html(HTML, task, task.url)
    assert result["questions"][0]["score"] == 0
    assert result["questions"][0]["points_possible"] == 10
    assert result["questions"][1]["score"] is None
    assert result["questions"][1]["score_availability"] == "unknown"
    assert result["rubric"][0]["text"] == "-2: Explanation incomplete"
    assert result["submission_content"][0]["text"] == "print(1)"
    assert result["complete"] is False
    assert result["outcome"] == "partial"
    assert result["collected_at"] == result["last_confirmed_at"]
    assert "SECRET" not in json.dumps(result)


def test_no_inference_from_scripts_or_downloaded_assets(task):
    result = parse_detail_html('<script>{"score":100}</script><img src="scan.png">', task, task.url)
    assert all(value["status"] == "unavailable" for value in result["field_availability"].values())
    assert result["questions"] == []


def test_question_and_text_budgets(task):
    rows = "<tr><td>" + "x" * 1000 + "</td><td>0 / 10</td></tr>"
    result = parse_detail_html(
        "<table><tr><th>Question</th><th>Score</th></tr>" + rows * 201 + "</table>", task, task.url
    )
    assert len(result["questions"]) == MAX_QUESTIONS
    assert result["truncated"]


@pytest.mark.parametrize(
    "suffix",
    [
        "/edit",
        "/submissions/new",
        "/submissions/3/edit",
        "?token=secret",
        "#start",
        "/../3",
        "/submissions/3/regrade",
        "/submissions/3.pdf",
    ],
)
def test_url_allowlist_rejects_actions_and_arbitrary_urls(task, suffix):
    assert not safe_detail_url(task.url + suffix, task, "https://www.gradescope.com")


def test_url_allowlist_rejects_other_origins_and_assignments(task):
    assert not safe_detail_url(
        task.url.replace("www.gradescope.com", "evil.test"), task, "https://www.gradescope.com"
    )
    assert not safe_detail_url(
        task.url.replace("assignments/2", "assignments/3"), task, "https://www.gradescope.com"
    )
    assert safe_detail_url(task.url + "/submissions/3/graded", task, "https://www.gradescope.com")


class Response:
    def __init__(self, html, status=200):
        self.status = status
        self.headers = {"content-type": "text/html"}
        self.html = html
        self.disposed = False

    async def body(self):
        return self.html.encode()

    async def dispose(self):
        self.disposed = True


class Request:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get(self, url, **kwargs):
        assert kwargs["max_redirects"] == 0
        self.calls.append(url)
        return self.responses[len(self.calls) - 1]


async def test_explicit_observed_submission_link_two_get_budget(task):
    first = Response('<a href="/courses/1/assignments/2/submissions/3">View graded submission</a>')
    second = Response(HTML)
    request = Request([first, second])
    result = await collect_task_detail(SimpleNamespace(request=request), task)
    assert result["requests_used"] == len(request.calls) == 2
    assert result["questions"][0]["score"] == 0
    assert first.disposed and second.disposed


@pytest.mark.parametrize(
    "status,outcome",
    [(302, "auth_required"), (403, "auth_required"), (429, "rate_limited"), (500, "network_error")],
)
async def test_redirect_permission_and_rate_failure_do_not_follow(task, status, outcome):
    request = Request([Response("", status)])
    result = await collect_task_detail(SimpleNamespace(request=request), task)
    assert result["outcome"] == outcome
    assert result["collected_at"] is None
    assert len(request.calls) == 1


async def test_ungraded_or_unobserved_task_never_networks(task):
    for change in ({"graded": False}, {"raw_data": {}}):
        result = await collect_task_detail(object(), task.model_copy(update=change))
        assert result["requests_used"] == 0
        assert result["collected_at"] is None


async def test_second_page_permission_failure_preserves_first_page_evidence(task):
    first = Response(
        HTML + '<a href="/courses/1/assignments/2/submissions/3">View graded submission</a>'
    )
    request = Request([first, Response("", 403)])
    result = await collect_task_detail(SimpleNamespace(request=request), task)
    assert result["outcome"] == "partial"
    assert result["questions"][0]["score"] == 0
    assert result["collected_at"] is not None
    assert "login_or_permission_required" in result["warnings"]


def test_targeted_store_preserves_scores_and_previous_detail_on_failure(tmp_path, task):
    db = Database(tmp_path / "isolated.sqlite3")
    course = Course(provider="gradescope", external_id="1", name="Fixture")
    result = SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task])
    db.apply("gradescope", result, now().isoformat())
    parsed = parse_detail_html(HTML, task, task.url)
    store_task_detail(db, task.id, parsed)
    failure = parsed | {"collected_at": None, "outcome": "auth_required", "questions": []}
    store_task_detail(db, task.id, failure)
    db.apply("gradescope", result, now().isoformat())
    saved = db.tasks()[0]
    assert saved["score"] == 0
    assert saved["graded"] is True
    assert saved["raw_data"]["gradescope_detail"] == parsed
    assert saved["raw_data"]["gradescope_detail_attempt"] == failure
    with pytest.raises(ValueError):
        store_task_detail(db, "gradescope:1:another", parsed)

    newer = parse_detail_html(
        '<section aria-label="Feedback">New feedback</section>', task, task.url
    )
    store_task_detail(db, task.id, newer)
    saved = db.tasks()[0]["raw_data"]["gradescope_detail"]
    assert saved["questions"] == parsed["questions"]
    assert saved["field_availability"]["questions"]["status"] == "stale"
    assert saved["feedback"][0]["text"] == "New feedback"


def test_repeated_partial_refresh_keeps_each_fields_original_confirmation(tmp_path, task):
    db = Database(tmp_path / "isolated.sqlite3")
    course = Course(provider="gradescope", external_id="1", name="Fixture")
    db.apply(
        "gradescope",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    original = parse_detail_html(HTML, task, task.url)
    original["collected_at"] = original["last_confirmed_at"] = "2026-10-01T10:00:00+00:00"
    store_task_detail(db, task.id, original)
    for hour in (11, 12, 13):
        latest = parse_detail_html(
            f'<section aria-label="Feedback">Feedback at {hour}</section>',
            task,
            task.url + "/submissions/3",
        )
        latest["collected_at"] = latest["last_confirmed_at"] = f"2026-10-01T{hour}:00:00+00:00"
        store_task_detail(db, task.id, latest)
        saved = db.tasks()[0]["raw_data"]["gradescope_detail"]
        for field in ("questions", "rubric", "submission_content"):
            assert saved[field] == original[field]
            availability = saved["field_availability"][field]
            assert availability["status"] == "stale"
            assert availability["last_confirmed_at"] == original["last_confirmed_at"]
            assert availability["source_url"] == task.url
        assert saved["last_confirmed_at"] == latest["last_confirmed_at"]
        assert saved["feedback"][0]["text"] == f"Feedback at {hour}"
