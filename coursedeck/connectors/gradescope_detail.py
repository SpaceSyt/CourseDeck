"""Bounded student-visible Gradescope detail reads; never execute page scripts.

This deliberately does not reverse engineer private APIs or assume that hidden
rubrics, scanned answers or client-rendered content are present in HTML.
"""

import json
import re
from copy import deepcopy
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError

from ..domain import Task, now
from .browser_base import is_login

MAX_HTML_BYTES = 2_000_000
MAX_TEXT = 40_000
MAX_QUESTIONS = 200
DETAIL_FIELDS = ("questions", "rubric", "feedback", "submission_content")
_NUMBER = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
_GRADE = re.compile(rf"^\s*({_NUMBER})\s*/\s*({_NUMBER})\s*$")


def safe_detail_url(url: str, task: Task, base_url: str) -> bool:
    """Allow only exact, observed student assignment/submission paths."""
    parsed, origin = urlparse(url), urlparse(base_url)
    prefix = f"/courses/{task.course_external_id}/assignments/{task.external_id}"
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == origin.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(re.escape(prefix) + r"(?:/submissions/\d+(?:/graded)?)?/?", parsed.path)
    )


def _result(task: Task, reason: str, outcome: str = "partial") -> dict:
    return {
        "schema_version": 1,
        "task_id": task.id,
        "source_url": task.url,
        "collected_at": None,
        "last_confirmed_at": None,
        "attempted_at": now().isoformat(),
        "outcome": outcome,
        "complete": False,
        "truncated": False,
        "questions": [],
        "rubric": [],
        "feedback": [],
        "submission_content": [],
        "field_availability": {
            name: {"status": "unavailable", "reason": reason, "source_url": task.url}
            for name in DETAIL_FIELDS
        },
        "warnings": [reason],
        "requests_used": 0,
    }


def parse_detail_html(html: str, task: Task, source_url: str) -> dict:
    """Extract explicit semantic HTML only; fixtures are not live-site validation."""
    result = _result(task, "not_present_in_readable_html")
    result["source_url"] = source_url
    if is_login(html):
        return _result(task, "login_required", "auth_required")
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, template, [hidden], [aria-hidden='true']"):
        node.decompose()
    result["collected_at"] = result["last_confirmed_at"] = now().isoformat()
    remaining = MAX_TEXT

    def bounded(text):
        nonlocal remaining
        value = text[:remaining]
        if len(value) < len(text):
            result["truncated"] = True
        remaining -= len(value)
        return value

    # Column labels establish meaning; no arithmetic inference from rubric points.
    for table in soup.find_all("table"):
        header = table.find("tr")
        if header is None:
            continue
        labels = [c.get_text(" ", strip=True).lower() for c in header.find_all(["th", "td"])]
        question_col = next((i for i, x in enumerate(labels) if x in {"question", "problem"}), None)
        score_col = next(
            (i for i, x in enumerate(labels) if x in {"score", "grade", "points"}), None
        )
        if question_col is None or score_col is None:
            continue
        possible_col = next(
            (i for i, x in enumerate(labels) if x in {"points possible", "out of", "maximum"}), None
        )
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"], recursive=False)
            if max(question_col, score_col) >= len(cells):
                continue
            title = cells[question_col].get_text(" ", strip=True)
            score_text = cells[score_col].get_text(" ", strip=True)
            grade = _GRADE.fullmatch(score_text)
            score = float(grade[1]) if grade else None
            possible = float(grade[2]) if grade else None
            if not grade and re.fullmatch(_NUMBER, score_text):
                score = float(score_text)
            if possible_col is not None and possible_col < len(cells):
                maximum = cells[possible_col].get_text(" ", strip=True)
                if re.fullmatch(_NUMBER, maximum):
                    possible = float(maximum)
            if not title:
                continue
            if len(result["questions"]) >= MAX_QUESTIONS:
                result["truncated"] = True
                break
            result["questions"].append(
                {
                    "label": bounded(title),
                    "score": score,
                    "points_possible": possible,
                    "score_text": bounded(score_text),
                    "source_url": source_url,
                    "score_availability": "known" if score is not None else "unknown",
                }
            )
    # Only explicit labelled sections: arbitrary page text is not submission content.
    section_names = {
        "rubric": "rubric",
        "grading rubric": "rubric",
        "feedback": "feedback",
        "instructor comments": "feedback",
        "submission specific comments": "feedback",
        "submission content": "submission_content",
        "your answer": "submission_content",
        "submitted code": "submission_content",
    }
    for section in soup.select("section, [role='region']"):
        heading = section.find(re.compile(r"^h[1-6]$"))
        label = str(
            section.get("aria-label") or (heading.get_text(" ", strip=True) if heading else "")
        )
        field = section_names.get(label.lower().strip())
        if field is None:
            continue
        content = section.get_text(" ", strip=True)
        if heading and content.startswith(heading.get_text(" ", strip=True)):
            content = content[len(heading.get_text(" ", strip=True)) :].strip()
        if content:
            result[field].append({"text": bounded(content), "source_url": source_url})
    for field in DETAIL_FIELDS:
        result["field_availability"][field] = {
            "status": "partial" if result[field] else "unavailable",
            "reason": "visible_html_only" if result[field] else "not_present_in_readable_html",
            "source_url": source_url,
        }
    result["warnings"] = [
        "Only explicit visible HTML was read. Dynamic views, hidden rubric items, "
        "image/PDF answers and additional question views were not fetched; coverage is partial."
    ]
    if result["truncated"]:
        result["warnings"].append("The text or question budget was reached.")
    return result


async def collect_task_detail(context, task: Task, *, base_url="https://www.gradescope.com"):
    """At most two GETs; use caller's existing authorized and serialized session.

    Only positively graded tasks qualify, to avoid opening a new assessment.
    Redirects and browser JavaScript are not followed/executed. No credentials
    are created or changed. Caller owns local persistence and provider locks.
    """
    if task.provider != "gradescope" or not task.graded or task.submission_status != "graded":
        return _result(task, "graded_submission_not_confirmed")
    if (
        not task.url
        or not task.raw_data.get("detail_url_observed")
        or not safe_detail_url(task.url, task, base_url)
    ):
        return _result(task, "no_safe_observed_detail_url")
    url = task.url
    first_result = None
    for number in range(1, 3):
        try:
            response = await context.request.get(url, timeout=45000, max_redirects=0)
            if response.status in {401, 403} or 300 <= response.status < 400:
                result = _result(task, "login_or_permission_required", "auth_required")
            elif response.status == 429:
                result = _result(task, "rate_limited", "rate_limited")
            elif response.status != 200:
                result = _result(task, "detail_page_unavailable", "network_error")
            elif "text/html" not in response.headers.get("content-type", "").lower():
                result = _result(task, "unsupported_content_type")
            else:
                body = await response.body()
                if len(body) > MAX_HTML_BYTES:
                    result = _result(task, "html_size_budget_exceeded")
                    result["truncated"] = True
                else:
                    html = body.decode("utf-8", errors="replace")
                    result = parse_detail_html(html, task, url)
                    # Follow only an explicitly labelled, observed submission view link.
                    if number == 1 and result["outcome"] != "auth_required":
                        soup = BeautifulSoup(html, "html.parser")
                        next_url = None
                        for link in soup.select("a[href]"):
                            if link.get_text(" ", strip=True).lower() not in {
                                "view graded submission",
                                "view submission",
                                "graded submission",
                            }:
                                continue
                            candidate = urljoin(url, str(link["href"]))
                            if (
                                candidate != url
                                and "/submissions/" in urlparse(candidate).path
                                and safe_detail_url(candidate, task, base_url)
                            ):
                                next_url = candidate
                                break
                        if next_url:
                            first_result = result
                            url = next_url
                            continue
            if first_result and any(first_result.get(field) for field in DETAIL_FIELDS):
                for field in DETAIL_FIELDS:
                    if not result[field] and first_result[field]:
                        result[field] = first_result[field]
                        result["field_availability"][field] = first_result["field_availability"][
                            field
                        ]
                if not result["collected_at"]:
                    result["collected_at"] = first_result["collected_at"]
                    result["last_confirmed_at"] = first_result["last_confirmed_at"]
                    result["outcome"] = "partial"
                result["truncated"] = result["truncated"] or first_result["truncated"]
            result["requests_used"] = number
            return result
        except BrowserError:
            result = _result(task, "browser_request_failed", "network_error")
            if first_result and any(first_result.get(field) for field in DETAIL_FIELDS):
                result = first_result
                result["warnings"].append("The linked submission page could not be read.")
            result["requests_used"] = number
            return result
        finally:
            if "response" in locals():
                await response.dispose()
                del response
    raise AssertionError("Unreachable detail request budget")


def store_task_detail(db, task_id: str, detail: dict) -> None:
    """Patch one task atomically; failed attempts never erase cached evidence.

    This does not update source sync freshness or completion state: observing a
    submission detail does not reconfirm the assignment list or its deadlines.
    """
    if detail.get("task_id") != task_id:
        raise ValueError("Detail belongs to another task")
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload FROM tasks WHERE id=? AND provider='gradescope'", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        payload = json.loads(row[0])
        raw = payload.setdefault("raw_data", {})
        raw["gradescope_detail_attempt"] = detail
        if detail.get("collected_at") and any(detail.get(field) for field in DETAIL_FIELDS):
            merged = deepcopy(detail)
            previous = raw.get("gradescope_detail", {})
            for field in DETAIL_FIELDS:
                if not merged.get(field) and previous.get(field):
                    merged[field] = previous[field]
                    previous_availability = previous.get("field_availability", {}).get(field, {})
                    merged["field_availability"][field] = {
                        "status": "stale",
                        "reason": "cached_not_reconfirmed",
                        "source_url": previous_availability.get(
                            "source_url", previous.get("source_url")
                        ),
                        "last_confirmed_at": previous_availability.get(
                            "last_confirmed_at", previous.get("last_confirmed_at")
                        ),
                    }
            raw["gradescope_detail"] = merged
        connection.execute(
            "UPDATE tasks SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), task_id),
        )
