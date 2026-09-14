"""Rephactor student assignments using the site's existing browser session."""

import hashlib
import math
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError

from ..credentials import CredentialStore
from ..domain import Course, Outcome, SyncResult, Task, TaskScope
from .browser_base import BrowserConnector
from .dates import source_date
from .http import TransportError

HOME = "https://www.rephactor.com"
DASHBOARD = HOME + "/#!/dashboard"


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) else None


def parse_course(value):
    if not isinstance(value, dict):
        raise ValueError("Unrecognized Rephactor course")
    key, title = value.get("courseId"), value.get("title")
    if (
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise ValueError("Rephactor course identity unavailable")
    return Course(
        provider="rephactor",
        external_id=key,
        name=title,
        section=value.get("courseNumber") or None,
        source_url=DASHBOARD,
        raw_metadata={"term": value.get("term")},
    )


def parse_assignments(records, grades, course):
    if not isinstance(records, list):
        raise ValueError("Rephactor assignment list was not recognized")
    tasks, warnings, seen = [], [], set()
    complete, excluded = True, 0
    for record in records:
        if not isinstance(record, dict) or type(record.get("published")) is not bool:
            warnings.append("An assignment's visibility could not be read.")
            complete = False
            continue
        if not isinstance(record.get("assignmentType"), str):
            warnings.append("An assignment's type could not be read.")
            complete = False
            continue
        if not record["published"] or record["assignmentType"] in {"AT", "EG"}:
            excluded += 1
            continue
        key, title = record.get("id"), record.get("assignmentName")
        if (
            type(key) is not int
            or key <= 0
            or key in seen
            or record.get("courseId") != course.external_id
            or not isinstance(title, str)
            or not title.strip()
        ):
            warnings.append(
                "An assignment had an invalid or duplicate identity; cached tasks retained."
            )
            complete = False
            continue
        seen.add(key)
        unavailable = ["submission_status", "graded", "available_at", "closes_at"]
        raw_due, due = record.get("dueDate"), None
        if isinstance(raw_due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw_due):
            try:
                due = source_date(raw_due, "UTC")
            except ValueError:
                unavailable.append("due_at")
        elif "dueDate" not in record or raw_due not in (None, ""):
            unavailable.append("due_at")
        score = number(grades.get(str(key))) if isinstance(grades, dict) else None
        points = number(record.get("maxPoints"))
        if score is None:
            unavailable.append("score")
        if points is None:
            unavailable.append("points_possible")
        description = record.get("description")
        if not isinstance(description, str):
            description = ""
            unavailable.append("description")
        if "due_at" in unavailable or "description" in unavailable:
            warnings.append(
                "Some assignment dates or descriptions could not be read; cached values retained."
            )
        tasks.append(
            Task(
                provider="rephactor",
                course_external_id=course.external_id,
                external_id=str(key),
                title=title.strip(),
                description=BeautifulSoup(description, "html.parser").get_text("\n", strip=True),
                due_at=due,
                points_possible=points,
                score=score,
                submission_status="unknown",
                url=f"{HOME}/#!/exerciseAssignment/{key}"
                if record.get("assignmentType") == "EX"
                else DASHBOARD,
                raw_data={
                    "assignment_type": record.get("assignmentType"),
                    "unavailable_fields": unavailable,
                },
            )
        )
    if tasks:
        warnings.append(
            "Rephactor completion is unverified; dates and scores do not establish completion."
        )
    return tasks, list(dict.fromkeys(warnings)), complete, excluded


class RephactorConnector(BrowserConnector):
    key = "rephactor"
    display_name = "Rephactor"
    default_url = HOME
    login_path = "/#!/dashboard"

    def __init__(self, db, browser, vault=None):
        super().__init__(db, browser)
        self.vault = vault or CredentialStore(db.path.parent)

    async def disconnect(self):
        await super().disconnect()
        self.vault.delete("rephactor_session")

    @property
    def base_url(self):
        return HOME

    @property
    def configuration_fields(self):
        return []

    @property
    def configuration_values(self):
        return {}

    async def configure(self, config):
        if config:
            raise ValueError("Rephactor has no source configuration")

    async def _user(self, context):
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(DASHBOARD, wait_until="domcontentloaded", timeout=45000)
        user = await self._get(context, "/getCurrentUser", {})
        if (
            not isinstance(user, dict)
            or not isinstance(user.get("email"), str)
            or not user["email"].strip()
            or user.get("role") != "student"
        ):
            raise TransportError(Outcome.AUTH_REQUIRED, "Sign in to a Rephactor student account.")
        fingerprint = hashlib.sha256(user["email"].strip().casefold().encode()).hexdigest()
        previous = self.db.state(self.key).get("account_fingerprint")
        if previous and previous != fingerprint:
            raise TransportError(
                Outcome.AUTH_REQUIRED, "Sign in to the original Rephactor account."
            )
        return user["email"], fingerprint

    async def _get(self, context, path, params):
        response = await context.request.get(
            HOME + path, params=params, timeout=30000, max_redirects=0
        )
        if response.status in {401, 403}:
            raise TransportError(Outcome.AUTH_REQUIRED, "Rephactor session expired. Reconnect.")
        if response.status == 429:
            raise TransportError(Outcome.RATE_LIMITED, "Rephactor rate limited this request.")
        if response.status >= 500:
            raise TransportError(Outcome.NETWORK_ERROR, "Rephactor is temporarily unavailable.")
        location = urlsplit(response.url)
        if response.status != 200 or (location.scheme, location.netloc, location.path) != (
            "https",
            "www.rephactor.com",
            path,
        ):
            raise TransportError(
                Outcome.PARSE_ERROR, "Rephactor returned an unrecognized response."
            )
        if path == "/getCurrentUser" and (await response.text()).strip() == "none":
            return None
        return await response.json()

    async def validate_session(self, context):
        try:
            email, fingerprint = await self._user(context)
            courses = await self._get(context, "/user/getUserActiveCourses", {"userId": email})
            if not isinstance(courses, list):
                return False
            for course in courses:
                parse_course(course)
            self.vault.set("rephactor_session", {"cookies": await context.cookies(HOME)})
            self.db.update_state(self.key, account_fingerprint=fingerprint)
            return True
        except (BrowserError, TransportError, ValueError):
            return False

    async def sync(self):
        result = SyncResult(
            outcome=Outcome.SUCCESS,
            metadata={
                "transport": "browser_session_api",
                "coverage": "published_student_assignments",
                "completion_verified": False,
            },
        )
        if self.connection_status() != "connected":
            return SyncResult(outcome=Outcome.AUTH_REQUIRED, warnings=["Connect Rephactor first."])
        try:
            async with self.browser.session("America/New_York") as context:
                session = self.vault.get("rephactor_session") or {}
                if session.get("cookies"):
                    await context.add_cookies(session["cookies"])
                email, _ = await self._user(context)
                courses = {}
                for path in ("/user/getUserActiveCourses", "/user/getUserInactiveCourses"):
                    try:
                        records = await self._get(context, path, {"userId": email})
                        if not isinstance(records, list):
                            raise ValueError("Unrecognized course list")
                        for record in records:
                            try:
                                course = parse_course(record)
                                courses[course.external_id] = course
                            except ValueError:
                                result.warnings.append(
                                    "A Rephactor course could not be identified."
                                )
                    except TransportError:
                        raise
                    except (BrowserError, ValueError):
                        result.warnings.append(
                            "A Rephactor course list could not be read; cached courses retained."
                        )
                result.courses = list(courses.values())
                excluded = 0
                for course in result.courses:
                    try:
                        records = await self._get(
                            context,
                            "/course/getAllAssignmentsInCourse",
                            {
                                "courseId": course.external_id,
                                "ascending": "true",
                            },
                        )
                        grade_error = None
                        try:
                            grades = await self._get(
                                context,
                                "/course/getAllGradesForStudent",
                                {
                                    "courseId": course.external_id,
                                    "userId": email,
                                },
                            )
                        except TransportError as exc:
                            grades, grade_error = None, exc
                        except (BrowserError, ValueError):
                            grades = None
                        if not isinstance(grades, dict):
                            result.warnings.append(
                                "Rephactor grades could not be read; cached scores retained."
                            )
                        tasks, warnings, covered, skipped = parse_assignments(
                            records, grades, course
                        )
                        result.tasks.extend(tasks)
                        result.warnings.extend(warnings)
                        excluded += skipped
                        if covered:
                            result.covered_task_scopes.append(
                                TaskScope(course_external_id=course.external_id)
                            )
                        if grade_error:
                            raise grade_error
                    except TransportError:
                        raise
                    except (BrowserError, ValueError):
                        result.warnings.append(
                            "A Rephactor assignment list could not be read; cached tasks retained."
                        )
                result.metadata["excluded_unpublished_or_grade_aggregates"] = excluded
                self.vault.set("rephactor_session", {"cookies": await context.cookies(HOME)})
        except TransportError as exc:
            result.outcome = Outcome.PARTIAL if result.tasks else exc.outcome
            result.warnings.append(exc.safe_message)
            result.metadata["interrupted_by"] = exc.outcome
        except BrowserError:
            result.outcome = Outcome.NETWORK_ERROR
            result.warnings.append("Rephactor could not be read; cached tasks retained.")
        except ValueError:
            result.outcome = Outcome.PARSE_ERROR
            result.warnings.append("Rephactor returned unrecognized data; cached tasks retained.")
        result.warnings = list(dict.fromkeys(result.warnings))
        if result.warnings and result.outcome == Outcome.SUCCESS:
            result.outcome = Outcome.PARTIAL
        return result
