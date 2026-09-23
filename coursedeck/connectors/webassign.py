"""Read WebAssign's student assignment lists through a dedicated browser session."""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from datetime import timezone as fixed_timezone
from time import monotonic
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError

from ..credentials import CredentialStore
from ..domain import Course, Outcome, SyncResult, Task, TaskScope
from ..session_resume import login_prompt_visible, resume_session
from .browser_base import BrowserConnector, is_login
from .dates import source_date
from .http import TransportError

PUBLIC_QUERY_KEYS = {
    "class",
    "classId",
    "classid",
    "section",
    "sectionId",
    "courseId",
    "course",
    "dep",
    "aid",
    "assignmentId",
}


def safe_page_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (
        parsed.hostname == "webassign.net" or (parsed.hostname or "").endswith(".webassign.net")
    ):
        raise ValueError("Only WebAssign student pages are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in source URLs")
    query = parse_qs(parsed.query)
    public_query = {k: v[0] for k, v in query.items() if k in PUBLIC_QUERY_KEYS}
    if query.get("action", [""])[0] in {
        "home/index",
        "assignments",
        "pastassignments",
        "futureassignments",
    }:
        public_query["action"] = query["action"][0]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode(public_query),
            "",
        )
    )


def parse_webassign(html: str, page_url: str, timezone: str | None, date_format: str | None = None):
    if is_login(html):
        raise TransportError(Outcome.AUTH_REQUIRED, "WebAssign requires login.")
    soup = BeautifulSoup(html, "html.parser")
    clean_url = safe_page_url(page_url)
    parsed = urlparse(clean_url)
    query = parse_qs(parsed.query)
    # Use source class ID where available, otherwise this stable page identity.
    cid = next(
        (
            query[k][0]
            for k in ["class", "classId", "classid", "section", "sectionId", "courseId", "course"]
            if query.get(k)
        ),
        hashlib.sha256(clean_url.encode()).hexdigest()[:16],
    )
    heading = soup.select_one(
        "#backNavBtn, #single-course-info > :last-child, .class-name, .course-name, #className"
    ) or soup.select_one("h1")
    course = Course(
        provider="webassign",
        external_id=cid,
        name=heading.get_text(" ", strip=True) if heading else "WebAssign course",
        source_url=clean_url,
        raw_metadata={"identity_source": "class_id" if query else "page_url"},
    )
    tasks, warnings = {}, []
    for row in soup.select("tr"):
        link = next(
            (
                a
                for a in row.select("a[href]")
                if re.search(r"[?&](dep|aid|assignmentId)=", str(a["href"]))
            ),
            None,
        )
        if link is None:
            continue
        assignment_url = safe_page_url(urljoin(clean_url, str(link["href"])))
        params = parse_qs(urlparse(assignment_url).query)
        aid = next(params[k][0] for k in ("dep", "aid", "assignmentId") if params.get(k))
        title = link.get_text(" ", strip=True)
        if not title:
            warnings.append("Skipped an assignment with no title.")
            continue
        # Match due-date semantics, never an arbitrary time elsewhere in a row.
        due_node = row.select_one("[data-due-date], .due-date, .dueDate")
        if due_node is None:
            table = row.find_parent("table")
            headers = table.select("thead th") if table else []
            cells = row.find_all(["th", "td"], recursive=False)
            due_index = next(
                (i for i, h in enumerate(headers) if "due" in h.get_text().lower()), None
            )
            if due_index is not None and due_index < len(cells):
                due_node = cells[due_index]
        raw_due = (
            (
                due_node.get("datetime")
                or due_node.get("data-due-date")
                or due_node.get_text(" ", strip=True)
            )
            if due_node
            else None
        )
        due = None
        if raw_due:
            try:
                due = webassign_date(raw_due, timezone, date_format)
            except ValueError:
                warnings.append(f"Assignment {aid}: due date needs verified format / timezone.")
        else:
            warnings.append(f"Assignment {aid}: due date was not found in the page.")
        status_node = row.select_one(
            "[data-submission-status], .submission-status, .assignment-status"
        )
        if status_node is None:
            table = row.find_parent("table")
            headers = table.select("thead th") if table else []
            cells = row.find_all(["th", "td"], recursive=False)
            status_index = next(
                (
                    i
                    for i, header in enumerate(headers)
                    if header.get_text(" ", strip=True).lower()
                    in {"status", "submission status", "assignment status"}
                ),
                None,
            )
            if status_index is not None and status_index < len(cells):
                status_node = cells[status_index]
        status_text = (
            str(status_node.get("data-submission-status") or status_node.get_text(" ", strip=True))
            if status_node
            else ""
        )
        # A score can reflect only some submitted questions. It cannot prove completion.
        status = {
            "completed": "completed",
            "complete": "completed",
            "submitted": "submitted",
            "not submitted": "open",
            "not started": "open",
            "in progress": "open",
            "incomplete": "open",
            "missing": "missing",
        }.get(status_text.strip().lower(), "unknown")
        unavailable = ["due_at"] if due is None else []
        if status == "unknown":
            unavailable.append("submission_status")
        restrictions = row.select_one('[data-test="restrictions"]')
        can_read_details = (
            "(Homework)" in row.get_text(" ", strip=True)
            and restrictions is not None
            and not restrictions.get_text(" ", strip=True)
            and not restrictions.select("svg, img, i, [title], [aria-label], [data-tooltip]")
            and urlparse(assignment_url).path == "/web/Student/Assignment-Responses/last"
        )
        tasks[aid] = Task(
            provider="webassign",
            course_external_id=cid,
            external_id=aid,
            title=title,
            due_at=due,
            url=assignment_url,
            submission_status=status,
            raw_data={
                "due_text": raw_due,
                "status_text": status_text,
                "unavailable_fields": unavailable,
                "detail_read_allowed": can_read_details,
            },
        )
    if not tasks and not recognized_empty_list(soup):
        raise TransportError(
            Outcome.PARSE_ERROR,
            "WebAssign assignment list was not recognized. Cached assignments retained.",
        )
    return course, list(tasks.values()), warnings


def parse_homework_submission(html: str) -> tuple[str, dict]:
    """Count actual submissions per part only after checking the whole question inventory."""
    soup = BeautifulSoup(html, "html.parser")
    if is_login(html):
        raise TransportError(Outcome.AUTH_REQUIRED, "WebAssign requires login.")
    summary = soup.select_one("#js-assignment-wrapper")
    questions = soup.select(".waQBox")
    if summary is None or not questions:
        return "unknown", {}
    inventory = []
    for link in summary.select("[aria-label]"):
        match = re.fullmatch(r"Question (\d+) of (\d+),?", str(link["aria-label"]).strip())
        if match:
            inventory.append((int(match[1]), int(match[2])))
    if not inventory:
        return "unknown", {}
    total = inventory[0][1]
    if (
        total < 1
        or len(inventory) != total
        or {item[1] for item in inventory} != {total}
        or {item[0] for item in inventory} != set(range(1, total + 1))
        or len(questions) != total
    ):
        return "unknown", {}
    parts = []
    question_numbers = set()
    for question in questions:
        heading = question.select_one('h2[aria-label^="Question "]')
        match = re.fullmatch(r"Question (\d+)", str(heading.get("aria-label"))) if heading else None
        if not match or int(match[1]) in question_numbers:
            return "unknown", {}
        question_numbers.add(int(match[1]))
        detail = question.select_one(".questionPartDetails")
        if detail is None:
            return "unknown", {}
        labels = [row.get_text(" ", strip=True) for row in detail.select(".columnLeft tr")]
        rows = detail.select(".columnContent table > tbody > tr")
        if labels != ["Question Part", "Points", "Submissions Used"] or len(rows) != 3:
            return "unknown", {}
        indexes = [cell.get_text(" ", strip=True) for cell in rows[0].select("td")]
        counts = rows[2].select("td.submissions")
        if (
            not counts
            or indexes != [str(i) for i in range(1, len(counts) + 1)]
            or len(rows[1].select("td")) != len(counts)
        ):
            return "unknown", {}
        for count in counts:
            match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", count.get_text(" ", strip=True))
            if not match or not 0 <= int(match[1]) <= int(match[2]) or int(match[2]) == 0:
                return "unknown", {}
            parts.append(int(match[1]))
    if question_numbers != set(range(1, total + 1)):
        return "unknown", {}
    evidence = {
        "kind": "question_part_submissions",
        "questions": total,
        "parts": len(parts),
        "submitted_parts": sum(count > 0 for count in parts),
    }
    current_statuses = [
        " ".join(
            str(node.get("data-submission-status") or node.get_text(" ", strip=True))
            .lower()
            .split()
        )
        for node in soup.select("[data-submission-status], .submission-status")
    ]
    if any(
        status in {"not submitted", "incomplete", "reopened", "needs revision"}
        for status in current_statuses
    ):
        return "open", evidence
    return ("submitted" if all(parts) else "open"), evidence


def homework_page_matches(url: str, assignment_url: str, assignment_id: str) -> bool:
    try:
        actual = urlparse(safe_page_url(url))
        expected = urlparse(safe_page_url(assignment_url))
        return (
            actual.scheme == expected.scheme == "https"
            and actual.hostname == expected.hostname
            and (actual.port or 443) == (expected.port or 443)
            and actual.path == "/web/Student/Assignment-Responses/last"
            and parse_qs(actual.query).get("dep") == [assignment_id]
        )
    except ValueError:
        return False


def recognized_empty_list(soup: BeautifulSoup) -> bool:
    scope = soup.select_one("#js-student-myAssignmentsPage")
    if scope is None:
        return False
    text = scope.get_text(" ", strip=True).lower()
    return "there are no assignments" in text or (
        "there are no current assignments" in text and "there are no past assignments" in text
    )


def assignment_list_complete(html: str, warnings: list[str]) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    scope = soup.select_one("#js-student-myAssignmentsPage")
    if warnings or scope is None:
        return False
    if not soup.select_one('[role="tab"][aria-label="Show All Assignments (selected)"]'):
        return False
    if scope.select_one(
        '.pagination, [aria-label*="pagination" i], [aria-label*="next page" i], '
        '[data-has-more="true"], a[rel="next"], input[type="search"][value]:not([value=""])'
    ):
        return False
    rows = scope.select("tbody tr")
    for row in rows:
        if not any(
            re.search(r"[?&](dep|aid|assignmentId)=", str(link["href"]))
            for link in row.select("a[href]")
        ):
            return False
    text = scope.get_text(" ", strip=True).lower()
    if recognized_empty_list(soup):
        return True
    # The selected All tab can precede the arrival of its past-assignment results.
    for category in ("current", "past"):
        tables = [
            table
            for table in scope.select("table")
            if (header := table.select_one("thead th")) is not None
            and header.get_text(" ", strip=True).lower() == f"{category} assignments"
        ]
        if not any(table.select("tbody tr") for table in tables) and (
            f"there are no {category} assignments" not in text
        ):
            return False
    return True


async def wait_for_rendered_assignments(page):
    await page.wait_for_function(
        """() => {
            const root = document.querySelector('#js-student-myAssignmentsPage');
            if (!root) return false;
            if (root.querySelector('[aria-busy="true"], [role="progressbar"]')) return false;
            const text = root.innerText.toLowerCase();
            if (text.includes('there are no assignments')) return true;
            return ['current', 'past'].every(category => {
                if (text.includes(`there are no ${category} assignments`)) return true;
                const tables = [...root.querySelectorAll('table')].filter(table =>
                    table.querySelector('thead th')?.innerText.trim().toLowerCase() ===
                        `${category} assignments`);
                return tables.some(table => {
                    const rows = [...table.querySelectorAll('tbody tr')];
                    return rows.length && rows.every(row =>
                        [...row.querySelectorAll('a[href]')].some(link =>
                            /[?&](dep|aid|assignmentId)=/.test(link.getAttribute('href'))));
                });
            });
        }""",
        timeout=25000,
    )


async def wait_for_assignment_list(page, selector: str):
    try:
        await page.locator(selector).first.wait_for(state="attached", timeout=25000)
    except BrowserError:
        # Check while the dedicated context is still open: redirects can finish after goto.
        expired = False
        try:
            safe_page_url(page.url)
            expired = is_login(await page.content()) or (
                "you have successfully been logged out"
                in (await page.locator("body").inner_text(timeout=1000)).lower()
            )
        except ValueError:
            expired = True
        except BrowserError:
            pass
        if expired:
            raise TransportError(
                Outcome.AUTH_REQUIRED,
                "WebAssign session expired. Reconnect; cached assignments retained.",
            ) from None
        raise


def webassign_date(value, timezone=None, date_format=None):
    if date_format:
        return source_date(datetime.strptime(value, date_format).isoformat(), timezone)
    try:
        return source_date(value, timezone)
    except ValueError:
        pass
    # These suffixes have unambiguous offsets; ambiguous abbreviations require a source zone.
    suffix = value.rsplit(" ", 1)[-1]
    offsets = {"UTC": 0, "GMT": 0, "EDT": -4, "EST": -5, "PDT": -7, "PST": -8}
    local_text = value.rsplit(" ", 1)[0] if suffix.isalpha() else value
    for pattern in ("%A, %B %d, %Y at %I:%M %p", "%a, %b %d, %Y, %I:%M %p"):
        try:
            local = datetime.strptime(local_text, pattern)
        except ValueError:
            continue
        if suffix in offsets:
            return local.replace(
                tzinfo=fixed_timezone(timedelta(hours=offsets[suffix]))
            ).astimezone(UTC)
        result = source_date(local.isoformat(), timezone)
        if suffix.isalpha():
            from zoneinfo import ZoneInfo

            if not timezone or result.astimezone(ZoneInfo(timezone)).tzname() != suffix:
                raise ValueError("Source timezone does not match the displayed date")
        return result
    raise ValueError("Unrecognized WebAssign date")


class WebAssignConnector(BrowserConnector):
    key = "webassign"
    display_name = "WebAssign"
    description = "WebAssign courses and assignment deadlines."
    default_url = "https://www.webassign.net"
    login_path = "/login.html"

    def __init__(self, db, browser, vault=None):
        super().__init__(db, browser)
        self.vault = vault or CredentialStore(db.path.parent)

    async def disconnect(self):
        await super().disconnect()
        self.vault.delete("webassign_session")

    def session_url(self, public_url):
        clean = safe_page_url(public_url)
        session = self.vault.get("webassign_session") or {}
        credential = parse_qs(urlparse(session.get("url", "")).query).get("UserPass")
        if not credential:
            raise TransportError(
                Outcome.AUTH_REQUIRED, "Finish WebAssign login to save the session."
            )
        parsed = urlparse(clean)
        query = parse_qs(parsed.query)
        query["UserPass"] = credential
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @property
    def configuration_fields(self):
        return super().configuration_fields + [
            {
                "key": "course_pages",
                "label": "Assignment list URLs (optional)",
                "type": "json",
                "placeholder": "[]",
                "help": "JSON list of your courses' My Assignments URLs.",
            },
            {
                "key": "date_format",
                "label": "Displayed date format (optional)",
                "type": "text",
                "placeholder": "%b %d, %Y %I:%M %p",
                "help": "Leave blank when the source provides ISO timestamps. See README.",
            },
        ]

    async def configure(self, config: dict):
        extra = {key: config[key] for key in ("course_pages", "date_format") if key in config}
        if "course_pages" in extra:
            if not isinstance(extra["course_pages"], list) or len(extra["course_pages"]) > 100:
                raise ValueError("Provide at most 100 course page URLs")
            extra["course_pages"] = [safe_page_url(url) for url in extra["course_pages"]]
        await super().configure({k: v for k, v in config.items() if k not in extra})
        self.db.update_state(self.key, config=self.config | extra)

    async def validate_session(self, context):
        for page in reversed(context.pages):
            try:
                url = safe_page_url(page.url)
            except ValueError:
                continue
            soup = BeautifulSoup(await page.content(), "html.parser")
            text = soup.get_text(" ", strip=True).lower()
            if not is_login(str(soup)) and ("my assignments" in text or "my classes" in text):
                if not parse_qs(urlparse(page.url).query).get("UserPass"):
                    continue
                self.vault.set(
                    "webassign_session",
                    {
                        "url": page.url,
                        "cookies": await context.cookies(),
                    },
                )
                self.db.update_state(self.key, landing_page=url)
                return True
        return False

    async def sync(self):
        if self.connection_status() != "connected":
            return SyncResult(outcome=Outcome.AUTH_REQUIRED, warnings=["Connect WebAssign first."])
        result = SyncResult(
            outcome=Outcome.SUCCESS,
            complete=False,
            metadata={"transport": "browser_dom", "coverage": "assignment_lists"},
        )
        urls = self.config.get("course_pages") or [self.db.state(self.key).get("landing_page")]
        if not all(urls):
            return SyncResult(
                outcome=Outcome.PARSE_ERROR,
                warnings=["Reconnect and open a course's My Assignments page."],
            )
        try:
            async with self.browser.session(self.config.get("timezone")) as context:
                detail_deadline = monotonic() + 60
                session = self.vault.get("webassign_session") or {}
                if session.get("cookies"):
                    await context.add_cookies(session["cookies"])
                page = await context.new_page()
                network_shapes = set()

                def observe(response):
                    # Record metadata only: never tokens, query values, payloads, or school content.
                    if response.request.resource_type in {"xhr", "fetch"}:
                        content_type = response.headers.get("content-type", "").split(";")[0]
                        network_shapes.add((response.status, content_type))

                page.on("response", observe)
                urls = list(dict.fromkeys(urls))
                for url in urls:
                    target = urlparse(url)

                    def at_assignment_list(candidate, target=target):
                        parsed = urlparse(candidate)
                        query = parse_qs(parsed.query)
                        return (parsed.scheme, parsed.netloc, parsed.path) == (
                            "https",
                            target.netloc,
                            target.path,
                        ) and all(
                            query.get(key) == value
                            for key, value in parse_qs(target.query).items()
                            if key in PUBLIC_QUERY_KEYS
                        )

                    response = await page.goto(
                        self.session_url(url), wait_until="domcontentloaded", timeout=45000
                    )
                    if response and response.status == 429:
                        raise TransportError(
                            Outcome.RATE_LIMITED, "WebAssign rate limited this request."
                        )
                    if not await resume_session(page, at_assignment_list):
                        landed = urlparse(page.url)
                        if (landed.scheme, landed.netloc, landed.path) == (
                            target.scheme,
                            target.netloc,
                            target.path,
                        ):
                            raise TransportError(
                                Outcome.PARSE_ERROR,
                                "WebAssign returned a different course; "
                                "cached assignments retained.",
                            )
                        needs_login = await login_prompt_visible(page) or is_login(
                            await page.content()
                        )
                        raise TransportError(
                            Outcome.AUTH_REQUIRED if needs_login else Outcome.NETWORK_ERROR,
                            "WebAssign redirected to login. Reconnect."
                            if needs_login
                            else "WebAssign sign-in redirect did not finish. Retry sync.",
                        )
                    await wait_for_assignment_list(
                        page,
                        "#js-student-myAssignmentsWrapper button, "
                        "#js-student-myAssignmentsPage [role='tab']",
                    )
                    # An SSO return can rotate UserPass while retaining the same course URL.
                    if parse_qs(urlparse(page.url).query).get("UserPass"):
                        await self.validate_session(context)
                    # Discover additional courses when the account exposes course links.
                    for href in await page.locator('a[href*="course="]').evaluate_all(
                        "elements => elements.map(element => element.href)"
                    ):
                        try:
                            candidate = safe_page_url(href)
                        except ValueError:
                            continue
                        query = parse_qs(urlparse(candidate).query)
                        if query.get("action") == ["home/index"] and candidate not in urls:
                            if len(urls) < 100:
                                urls.append(candidate)
                    home_button = page.locator("#js-student-myAssignmentsWrapper").get_by_role(
                        "button", name=re.compile(r"^(?:Show )?Current Assignments(?:\s*\(\d+\))?$")
                    )
                    if await home_button.count():
                        await home_button.click()
                        await wait_for_assignment_list(
                            page,
                            "#js-student-myAssignmentsPage [role='tab'], "
                            "[role='tab'][aria-label='Show All Assignments']",
                        )
                    await page.get_by_role(
                        "tab", name=re.compile(r"^Show All Assignments(?: \(selected\))?$")
                    ).wait_for(timeout=15000)
                    all_tab = page.get_by_role("tab", name="Show All Assignments", exact=True)
                    if await all_tab.count():
                        await all_tab.click()
                        await page.get_by_role(
                            "tab", name="Show All Assignments (selected)", exact=True
                        ).wait_for(timeout=15000)
                    await wait_for_rendered_assignments(page)
                    html = await page.content()
                    course, tasks, warnings = parse_webassign(
                        html, url, self.config.get("timezone"), self.config.get("date_format")
                    )
                    result.courses.append(course)
                    result.tasks.extend(tasks)
                    result.warnings.extend(warnings)
                    if assignment_list_complete(html, warnings):
                        result.covered_task_scopes.append(
                            TaskScope(course_external_id=course.external_id)
                        )
                    # Only the verified ordinary-homework layout can be opened automatically.
                    for task in tasks:
                        if (
                            task.submission_status != "unknown"
                            or not task.raw_data.get("detail_read_allowed")
                            or monotonic() >= detail_deadline
                        ):
                            continue
                        detail = await context.new_page()
                        try:
                            await detail.goto(
                                self.session_url(task.url),
                                wait_until="domcontentloaded",
                                timeout=12000,
                            )
                            if not homework_page_matches(detail.url, task.url, task.external_id):
                                continue
                            await detail.locator(
                                '#js-assignment-wrapper [aria-label^="Question 1 of "]'
                            ).first.wait_for(state="attached", timeout=5000)
                            status, evidence = parse_homework_submission(await detail.content())
                            if status != "unknown":
                                task.submission_status = status
                                task.raw_data["submission_evidence"] = evidence
                                task.raw_data["unavailable_fields"] = [
                                    field
                                    for field in task.raw_data["unavailable_fields"]
                                    if field != "submission_status"
                                ]
                        except (BrowserError, TransportError):
                            # The list and its deadlines remain usable if details cannot be read.
                            pass
                        finally:
                            await detail.close()
                self.vault.set(
                    "webassign_session",
                    (self.vault.get("webassign_session") or session)
                    | {"cookies": await context.cookies()},
                )
                result.metadata["network_response_shapes"] = [
                    list(item) for item in sorted(network_shapes)
                ]
        except TransportError as exc:
            result.warnings.append(exc.safe_message)
            result.outcome = Outcome.PARTIAL if result.tasks else exc.outcome
        except BrowserError:
            result.warnings.append(
                "WebAssign page did not load reliably. Cached assignments retained."
            )
            result.outcome = Outcome.PARTIAL if result.tasks else Outcome.NETWORK_ERROR
        unknown = sum(task.submission_status == "unknown" for task in result.tasks)
        if unknown:
            result.warnings.append(
                f"Submission status is unavailable for {unknown} WebAssign assignments."
            )
        if result.warnings and result.outcome == Outcome.SUCCESS:
            result.outcome = Outcome.PARTIAL
        if not result.courses and result.outcome in {Outcome.SUCCESS, Outcome.PARTIAL}:
            result.outcome = Outcome.PARSE_ERROR
            result.warnings.append("Could not read the WebAssign assignment list. Retry sync.")
        return result
