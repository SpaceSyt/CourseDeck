"""Read-only quiz records; publication/availability dates are never invented deadlines.

API shapes: https://docs.valence.desire2learn.com/res/quiz.html
"""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from ..domain import Course, Outcome, SyncResult, Task, TaskScope
from .dates import source_date
from .http import TransportError


async def object_pages(get, base_url: str, endpoint: str, params: dict | None = None):
    """Yield pages without losing already-read records if a later request fails."""
    path, seen = endpoint, set()
    required = {str(key): str(value) for key, value in (params or {}).items()}
    for _ in range(1000):
        body = await get(path, params if path == endpoint else None)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("Objects"), list)
            or "Next" not in body
        ):
            raise TransportError(Outcome.PARSE_ERROR, "Activity page was not recognized.")
        yield body["Objects"]
        link = body["Next"]
        if link is None:
            return
        if not isinstance(link, str) or not link:
            raise TransportError(Outcome.PARSE_ERROR, "Activity pagination link was invalid.")
        target = urlparse(urljoin(base_url + path, link))
        base = urlparse(base_url)
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or target.path != endpoint
            or target.username
            or target.password
            or target.fragment
        ):
            raise TransportError(Outcome.PARSE_ERROR, "Activity pagination left its endpoint.")
        query = parse_qs(target.query, keep_blank_values=True)
        # Never let an attempts pagination link broaden the request to other users.
        for key, value in required.items():
            if key in query and query[key] != [value]:
                raise TransportError(Outcome.PARSE_ERROR, "Activity pagination changed its filter.")
            query[key] = [value]
        path = target.path + ("?" + urlencode(query, doseq=True) if query else "")
        if path in seen:
            raise TransportError(Outcome.PARSE_ERROR, "Activity pagination did not advance.")
        seen.add(path)
    raise TransportError(Outcome.PARSE_ERROR, "Activity pagination safety limit reached.")


def map_quiz(course: Course, raw: dict) -> Task:
    quiz_id = str(raw["QuizId"])
    if not quiz_id.isdecimal():
        raise ValueError("Invalid quiz identifier")
    dates, unavailable = {}, ["submission_status", "graded", "score"]
    for field, source in (
        ("due_at", "DueDate"),
        ("available_at", "StartDate"),
        ("closes_at", "EndDate"),
    ):
        try:
            if source not in raw:
                raise ValueError("Date field missing")
            dates[field] = source_date(raw[source])
        except (TypeError, ValueError):
            dates[field] = None
            unavailable.append(field)
    text = []
    for field in ("Description", "Instructions"):
        block = raw.get(field) or {}
        if block.get("IsDisplayed") is True:
            rich = block.get("Text") or {}
            text.append(
                rich.get("Text")
                or BeautifulSoup(rich.get("Html", ""), "html.parser").get_text(" ", strip=True)
            )
    return Task(
        provider="brightspace",
        external_id=f"quiz-{quiz_id}",
        course_external_id=course.external_id,
        title=raw["Name"],
        description="\n\n".join(part for part in text if part),
        **dates,
        url=f"{urlparse(course.source_url).scheme}://{urlparse(course.source_url).netloc}"
        f"/d2l/lms/quizzing/user/quiz_summary.d2l?qi={quiz_id}&ou={course.external_id}",
        submission_status="unknown",
        raw_data={
            "parser": "quiz_api",
            "unavailable_fields": unavailable,
            "submission_summary": {"known": False},
        },
    )


def apply_quiz_attempts(task: Task, attempts: list[dict], user_id: str) -> None:
    """Only a fully read, identity-checked list can establish no submission."""
    quiz_id = task.external_id.removeprefix("quiz-")
    completed = []
    for attempt in attempts:
        if str(attempt.get("UserId")) != user_id or str(attempt.get("QuizId")) != quiz_id:
            raise ValueError("Attempt identity did not match the current user and quiz")
        if "Completed" not in attempt:
            raise ValueError("Attempt completion was missing")
        when = source_date(attempt["Completed"])
        if when:
            completed.append((when, attempt))
    latest = max(completed, key=lambda item: item[0])[1] if completed else None
    published = bool(
        latest and latest.get("IsPublished") is True and latest.get("Score") is not None
    )
    # A quiz attempt score need not equal the overall quiz score (multiple attempts).
    # Preserve only submission evidence, not a misleading aggregate grade.
    task.submission_status = "graded" if published else "submitted" if completed else "open"
    task.graded = published
    task.raw_data["unavailable_fields"] = [
        field
        for field in task.raw_data.get("unavailable_fields", [])
        if field not in {"submission_status", "graded"}
    ]
    task.raw_data["submission_summary"] = {
        "known": True,
        "submitted": bool(completed),
        "graded": published,
    }


def parse_quiz_summary(html: str) -> dict:
    """Read the native Attempts field, never completion claims in instructions."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for heading in soup.select("h3.dhdg_2"):
        if heading.get_text(" ", strip=True) != "Attempts":
            continue
        if heading.find_parent(class_="d2l-htmlblock-untrusted"):
            continue
        section = heading.find_previous_sibling("h2")
        if section is None or section.get_text(" ", strip=True) != "Quiz Details":
            continue
        block = heading.find_next_sibling()
        if block is None or block.name != "div":
            continue
        match = re.fullmatch(
            r"Allowed\s*-\s*(?:\d+|Unlimited),\s*Completed\s*-\s*(\d+)",
            block.get_text(" ", strip=True),
            re.IGNORECASE,
        )
        if match:
            found.append(int(match[1]))
    if len(found) != 1:
        return {"known": False, "submission_status": "unknown"}
    count = found[0]
    return {
        "known": True,
        "completed_attempts": count,
        "submission_status": "submitted" if count else "open",
    }


class BrightspaceActivities:
    def __init__(
        self,
        get,
        base_url: str,
        le_version: str,
        user_id: str | None = None,
        known_activity_ids: set[tuple[str, str]] | None = None,
        quiz_status=None,
    ):
        self.get, self.base_url, self.le = get, base_url, le_version
        self.user_id = str(user_id) if user_id is not None else None
        self.known_activity_ids = known_activity_ids or set()
        self.quiz_status = quiz_status

    async def quiz_status_fallback(self, course: Course, raw: dict, task: Task) -> bool:
        if self.quiz_status is None:
            return False
        try:
            summary = await self.quiz_status(course, raw)
            count = summary.get("completed_attempts")
            if summary.get("known") is not True or type(count) is not int or count < 0:
                return False
            task.submission_status = "submitted" if count else "open"
            task.graded = False
            task.raw_data["unavailable_fields"] = [
                field
                for field in task.raw_data.get("unavailable_fields", [])
                if field != "submission_status"
            ]
            task.raw_data["submission_summary"] = {
                "known": True,
                "submitted": count > 0,
                "graded": False,
                "completed_attempts": count,
                "evidence": "quiz_summary",
            }
            return True
        except (TransportError, KeyError, ValueError, TypeError, AttributeError):
            return False

    async def discussions(self, course: Course, result: SyncResult, version: str = "1.90"):
        # Topic.DueDate was added in LE 1.90. EndDate only controls availability.
        # https://docs.valence.desire2learn.com/res/discuss.html
        endpoint = f"/d2l/api/le/{version}/{course.external_id}/discussions/forums/"
        records_complete = True
        try:
            forums = await self.get(endpoint, None)
            if not isinstance(forums, list):
                raise TransportError(Outcome.PARSE_ERROR, "Discussion forums were not recognized.")
            for forum in forums:
                try:
                    forum_id = str(forum["ForumId"])
                    if not forum_id.isdecimal() or not isinstance(forum.get("IsHidden"), bool):
                        raise ValueError("Forum visibility was not recognized")
                    if forum["IsHidden"]:
                        continue
                    topics = await self.get(endpoint + forum_id + "/topics/", None)
                    if not isinstance(topics, list):
                        raise TransportError(
                            Outcome.PARSE_ERROR, "Discussion topics were not recognized."
                        )
                    for raw in topics:
                        try:
                            topic_id = str(raw["TopicId"])
                            if (
                                not topic_id.isdecimal()
                                or str(raw["ForumId"]) != forum_id
                                or not isinstance(raw.get("IsHidden"), bool)
                            ):
                                raise ValueError("Topic identity or visibility was not recognized")
                            if raw["IsHidden"]:
                                continue
                            if "DueDate" not in raw:
                                raise ValueError("Discussion due date field was unavailable")
                            external_id = f"discussion-{topic_id}"
                            if (
                                raw["DueDate"] is None
                                and (course.external_id, external_id) not in self.known_activity_ids
                            ):
                                continue
                            dates, unavailable = {}, ["submission_status", "graded", "score"]
                            for field, source in (
                                ("due_at", "DueDate"),
                                ("available_at", "StartDate"),
                                ("closes_at", "EndDate"),
                            ):
                                try:
                                    dates[field] = source_date(raw[source])
                                except (KeyError, ValueError, TypeError):
                                    dates[field] = None
                                    unavailable.append(field)
                            description = raw.get("Description") or {}
                            task = Task(
                                provider="brightspace",
                                external_id=external_id,
                                course_external_id=course.external_id,
                                title=raw["Name"],
                                description=description.get("Text")
                                or BeautifulSoup(
                                    description.get("Html", ""), "html.parser"
                                ).get_text(" ", strip=True),
                                **dates,
                                submission_status="unknown",
                                url=f"{self.base_url}/d2l/le/{course.external_id}/discussions/topics/{topic_id}/View",
                                raw_data={
                                    "parser": "discussion_api",
                                    "unavailable_fields": unavailable,
                                },
                            )
                            result.tasks.append(task)
                            if set(unavailable) & {"due_at", "available_at", "closes_at"}:
                                result.warnings.append(
                                    f"{course.name}: {task.title} — "
                                    "discussion dates could not be fully read."
                                )
                        except (KeyError, ValueError, TypeError, AttributeError):
                            records_complete = False
                            result.warnings.append(
                                f"{course.name}: a discussion topic could not be fully read."
                            )
                except TransportError as exc:
                    records_complete = False
                    result.warnings.append(f"{course.name}: discussion topics — {exc.safe_message}")
                except (KeyError, ValueError, TypeError, AttributeError):
                    records_complete = False
                    result.warnings.append(f"{course.name}: a discussion forum could not be read.")
            if records_complete:
                result.covered_task_scopes.append(
                    TaskScope(
                        course_external_id=course.external_id, external_id_prefix="discussion-"
                    )
                )
        except TransportError as exc:
            result.warnings.append(f"{course.name}: discussions — {exc.safe_message}")

    async def quizzes(self, course: Course, result: SyncResult):
        endpoint = f"/d2l/api/le/{self.le}/{course.external_id}/quizzes/"
        records_complete = True
        try:
            async for page in object_pages(self.get, self.base_url, endpoint):
                for raw in page:
                    try:
                        if not isinstance(raw, dict) or not isinstance(raw.get("IsActive"), bool):
                            raise ValueError("Quiz visibility was not recognized")
                        if raw["IsActive"] is False:
                            continue
                        task = map_quiz(course, raw)
                    except (KeyError, ValueError, TypeError, AttributeError):
                        records_complete = False
                        result.warnings.append(f"{course.name}: a quiz could not be read.")
                        continue
                    result.tasks.append(task)
                    if set(task.raw_data["unavailable_fields"]) & {
                        "due_at",
                        "available_at",
                        "closes_at",
                    }:
                        result.warnings.append(
                            f"{course.name}: {task.title} — quiz dates could not be fully read."
                        )
                    if self.user_id is None or not self.user_id.isdecimal():
                        if await self.quiz_status_fallback(course, raw, task):
                            continue
                        result.warnings.append(
                            f"{course.name}: {task.title} — quiz submission identity unavailable."
                        )
                        continue
                    try:
                        attempts = []
                        async for attempt_page in object_pages(
                            self.get,
                            self.base_url,
                            endpoint + task.external_id.removeprefix("quiz-") + "/attempts/",
                            {"userId": self.user_id},
                        ):
                            attempts.extend(attempt_page)
                        apply_quiz_attempts(task, attempts, self.user_id)
                    except TransportError as exc:
                        if await self.quiz_status_fallback(course, raw, task):
                            continue
                        result.warnings.append(
                            f"{course.name}: {task.title} — "
                            f"quiz submission status: {exc.safe_message}"
                        )
                    except (KeyError, ValueError, TypeError, AttributeError):
                        if await self.quiz_status_fallback(course, raw, task):
                            continue
                        result.warnings.append(
                            f"{course.name}: {task.title} — quiz attempts could not be read."
                        )
            if records_complete:
                result.covered_task_scopes.append(
                    TaskScope(course_external_id=course.external_id, external_id_prefix="quiz-")
                )
        except TransportError as exc:
            result.warnings.append(f"{course.name}: quizzes — {exc.safe_message}")
