import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError

from ..domain import Course, Outcome, SyncResult, Task, TaskScope
from ..session_resume import login_prompt_visible, resume_session
from .browser_base import BrowserConnector, is_login, session_html
from .dates import source_date
from .http import TransportError


def parse_courses(html: str, base_url: str) -> list[Course]:
    if is_login(html):
        raise TransportError(Outcome.AUTH_REQUIRED, "Gradescope requires login.")
    soup = BeautifulSoup(html, "html.parser")
    courses = {}
    for link in soup.select('a[href*="/courses/"]'):
        match = re.fullmatch(r"/courses/(\d+)/?", urlparse(str(link.get("href"))).path)
        if not match:
            continue
        title_node = link.select_one(".courseBox--name") or link
        title = title_node.get_text(" ", strip=True)
        if title:
            courses[match[1]] = Course(
                provider="gradescope",
                external_id=match[1],
                name=title,
                source_url=f"{base_url}/courses/{match[1]}",
            )
    if not courses:
        raise TransportError(
            Outcome.PARSE_ERROR,
            "No recognizable Gradescope courses. "
            "Empty account or changed page; saved data retained.",
        )
    return list(courses.values())


def parse_assignments(
    html: str, course: Course, timezone: str | None = None
) -> tuple[list[Task], list[str]]:
    if is_login(html):
        raise TransportError(Outcome.AUTH_REQUIRED, "Gradescope requires login.")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#assignments-student-table")
    if table is None:
        # Restrict fallback to a table explicitly labeled as an assignment table.
        table = next(
            (
                t
                for t in soup.find_all("table")
                if "assignment" in t.get_text(" ", strip=True).lower()
            ),
            None,
        )
    if table is None:
        raise TransportError(Outcome.PARSE_ERROR, "Gradescope assignment table was not recognized.")
    tasks, warnings = {}, []
    for row in table.select("tbody tr") or table.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2 or not row.find("td"):
            continue
        first = cells[0]
        link = first.select_one('a[href*="/assignments/"]')
        button = first.select_one("[data-assignment-id]")
        match = re.search(r"/assignments/(\d+)", str(link.get("href"))) if link else None
        aid = match[1] if match else str(button["data-assignment-id"]) if button else None
        title = first.get_text(" ", strip=True)
        if not aid or not title:
            warnings.append("Skipped an assignment without a stable ID or title.")
            continue
        status_text = cells[1].get_text(" ", strip=True)
        grade = re.fullmatch(r"\s*([\d.]+)\s*/\s*([\d.]+)\s*", status_text)
        status = "unknown"
        lowered = status_text.lower()
        if grade:
            status = "graded"
        elif any(
            s in lowered
            for s in ["no submission", "not submitted", "unsubmitted", "awaiting submission"]
        ):
            status = "open"
        elif "missing" in lowered:
            status = "missing"
        elif re.fullmatch(r"submitted(?:\s*\(?late\)?)?", lowered):
            status = "submitted"
        elif lowered == "graded":
            status = "graded"
        date_nodes = row.select(".submissionTimeChart--dueDate[datetime]")
        release = row.select_one(".submissionTimeChart--releaseDate[datetime]")
        raw_dates = {
            "due_at": date_nodes[0].get("datetime") if date_nodes else None,
            "closes_at": date_nodes[1].get("datetime") if len(date_nodes) > 1 else None,
            "available_at": release.get("datetime") if release else None,
        }
        dates, unavailable = {}, []
        for field, value in raw_dates.items():
            if value is None:
                unavailable.append(field)
            try:
                dates[field] = source_date(value, timezone)
            except ValueError:
                unavailable.append(field)
                warnings.append(
                    f"Assignment {aid}: {field} could not be normalized; check source timezone."
                )
                dates[field] = None
        tasks[aid] = Task(
            provider="gradescope",
            course_external_id=course.external_id,
            external_id=aid,
            title=title,
            **dates,
            submission_status=status,
            graded=status == "graded",
            score=float(grade[1]) if grade else None,
            points_possible=float(grade[2]) if grade else None,
            url=urljoin(course.source_url, str(link["href"]))
            if link
            else f"{course.source_url}/assignments/{aid}",
            raw_data={
                "status_text": status_text,
                "dates": raw_dates,
                "unavailable_fields": unavailable,
            },
        )
    return list(tasks.values()), warnings


def assignment_list_complete(html: str, warnings: list[str]) -> bool:
    """Only the known unfiltered, unpaginated student table covers absent assignments."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#assignments-student-table")
    if warnings or table is None:
        return False
    if soup.select_one(
        '.pagination, .dataTables_paginate, [aria-label*="pagination" i], '
        '[aria-label*="next page" i], a[rel="next"], '
        'input[type="search"][value]:not([value=""])'
    ):
        return False
    if table.select_one('[data-filtered="true"], [data-has-more="true"], [aria-busy="true"]'):
        return False
    return all(
        row.select_one('a[href*="/assignments/"], [data-assignment-id]') is not None
        for row in table.select("tbody tr")
    )


class GradescopeConnector(BrowserConnector):
    key = "gradescope"
    display_name = "Gradescope"
    description = "Gradescope student assignments, deadlines and submission status."
    default_url = "https://www.gradescope.com"
    login_path = "/account"

    async def validate_session(self, context):
        try:
            html = await session_html(context, self.base_url + "/account")
            soup = BeautifulSoup(html, "html.parser")
            return bool(soup.select_one('a[href*="/courses/"], a[href*="logout"]'))
        except TransportError:
            return False

    async def sync(self):
        if self.connection_status() != "connected":
            return SyncResult(outcome=Outcome.AUTH_REQUIRED, warnings=["Connect Gradescope first."])
        result = SyncResult(
            outcome=Outcome.SUCCESS,
            complete=False,
            metadata={"transport": "browser_dom", "coverage": "student_course_pages"},
        )
        try:
            async with self.browser.session(self.config.get("timezone")) as context:
                page = await context.new_page()
                await page.goto(
                    self.base_url + "/account", wait_until="domcontentloaded", timeout=45000
                )
                origin = urlparse(self.base_url)

                def at_account(candidate):
                    parsed = urlparse(candidate)
                    return (
                        parsed.scheme == "https"
                        and parsed.netloc == origin.netloc
                        and parsed.path.rstrip("/") == "/account"
                    )

                if not await resume_session(page, at_account):
                    needs_login = await login_prompt_visible(page) or is_login(await page.content())
                    raise TransportError(
                        Outcome.AUTH_REQUIRED if needs_login else Outcome.NETWORK_ERROR,
                        "Gradescope requires login. Reconnect."
                        if needs_login
                        else "Gradescope sign-in redirect did not finish. Retry sync.",
                    )
                await page.locator('a[href*="/courses/"], input[type="password"]').first.wait_for(
                    state="attached", timeout=25000
                )
                result.courses = parse_courses(await page.content(), self.base_url)
                for course in result.courses:
                    try:
                        response = await page.goto(
                            course.source_url, wait_until="domcontentloaded", timeout=45000
                        )
                        if response and response.status == 429:
                            raise TransportError(
                                Outcome.RATE_LIMITED, "Gradescope rate limited this request."
                            )
                        if is_login(await page.content()):
                            raise TransportError(
                                Outcome.AUTH_REQUIRED, "Gradescope requires login."
                            )
                        await page.locator("#assignments-student-table").wait_for(
                            state="attached", timeout=25000
                        )
                        html = await page.content()
                        tasks, warnings = parse_assignments(
                            html, course, self.config.get("timezone")
                        )
                        result.tasks.extend(tasks)
                        result.warnings.extend(warnings)
                        if assignment_list_complete(html, warnings):
                            result.covered_task_scopes.append(
                                TaskScope(course_external_id=course.external_id)
                            )
                    except TransportError as exc:
                        result.warnings.append(exc.safe_message)
                        result.outcome = Outcome.PARTIAL
                    except BrowserError:
                        result.warnings.append(
                            "A Gradescope course page did not load reliably. Cached tasks retained."
                        )
                        result.outcome = Outcome.PARTIAL
        except TransportError as exc:
            result.outcome = Outcome.PARTIAL if result.tasks else exc.outcome
            result.warnings.append(exc.safe_message)
        except BrowserError:
            result.outcome = Outcome.PARTIAL if result.tasks else Outcome.NETWORK_ERROR
            result.warnings.append(
                "Browser request failed. Check your connection and Chromium installation."
            )
        if result.warnings and result.outcome == Outcome.SUCCESS:
            result.outcome = Outcome.PARTIAL
        # Account views can omit old courses; never authorize automatic archive from HTML coverage.
        return result
