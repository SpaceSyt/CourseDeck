"""Read WebAssign's student assignment lists through a dedicated browser session."""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from datetime import timezone as fixed_timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError

from ..credentials import CredentialStore
from ..domain import Course, Outcome, SyncResult, Task
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
        tasks[aid] = Task(
            provider="webassign",
            course_external_id=cid,
            external_id=aid,
            title=title,
            due_at=due,
            url=assignment_url,
            submission_status="unknown",
            raw_data={"due_text": raw_due, "unavailable_fields": ["due_at"] if due is None else []},
        )
    return course, list(tasks.values()), warnings


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
            outcome=Outcome.PARTIAL,
            complete=False,
            warnings=["Submission status is unavailable from the WebAssign assignment list."],
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
                    response = await page.goto(
                        self.session_url(url), wait_until="domcontentloaded", timeout=45000
                    )
                    if response and response.status == 429:
                        raise TransportError(
                            Outcome.RATE_LIMITED, "WebAssign rate limited this request."
                        )
                    try:
                        safe_page_url(page.url)
                    except ValueError as exc:
                        raise TransportError(
                            Outcome.AUTH_REQUIRED, "WebAssign redirected to login. Reconnect."
                        ) from exc
                    await page.locator(
                        "#js-student-myAssignmentsWrapper button, "
                        "#js-student-myAssignmentsPage section, "
                        "#js-student-myAssignmentsPage table, table"
                    ).first.wait_for(state="attached", timeout=25000)
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
                    home_button = page.locator("#js-student-myAssignmentsWrapper button")
                    if await home_button.count():
                        await home_button.click()
                        await page.locator(
                            "#js-student-myAssignmentsPage section, "
                            "#js-student-myAssignmentsPage table"
                        ).first.wait_for(state="attached", timeout=25000)
                    all_tab = page.get_by_role("tab", name="Show All Assignments", exact=True)
                    if await all_tab.count():
                        await all_tab.click()
                        await page.get_by_role(
                            "tab", name="Show All Assignments (selected)", exact=True
                        ).wait_for(timeout=15000)
                    html = await page.content()
                    course, tasks, warnings = parse_webassign(
                        html, url, self.config.get("timezone"), self.config.get("date_format")
                    )
                    result.courses.append(course)
                    result.tasks.extend(tasks)
                    result.warnings.extend(warnings)
                self.vault.set("webassign_session", session | {"cookies": await context.cookies()})
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
        if not result.courses and result.outcome == Outcome.PARTIAL:
            result.outcome = Outcome.PARSE_ERROR
            result.warnings.append("Could not read the WebAssign assignment list. Retry sync.")
        return result
