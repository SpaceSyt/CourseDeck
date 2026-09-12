"""Read visible Classroom pages in a dedicated, user-authenticated browser.

HTML coverage is deliberately incomplete: it must never authorize absence-based
archival. Requests are made by Classroom itself; cookies stay in the profile.
"""

import asyncio
import base64
import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as BrowserError

from ..domain import Course, Outcome, SyncResult, Task
from .browser_base import BrowserConnector
from .classroom_webdata import assignment_timing
from .http import TransportError

ORIGIN = "https://classroom.google.com"
COURSE_PATH = re.compile(r"/(?:u/\d+/)?c/([A-Za-z0-9_-]+)/?")
TASK_PATH = re.compile(r"/(?:u/\d+/)?c/([A-Za-z0-9_-]+)/a/([A-Za-z0-9_-]+)/details/?")
LINKS_JS = """() => [...document.querySelectorAll('a[href]')].map(a => ({
  href: a.href, text: a.innerText.trim(), label: a.getAttribute('aria-label') || '',
  course_heading: a.closest('li[data-course-id]')?.querySelector('h2')?.innerText || ''
})).filter(a => a.href.startsWith('https://classroom.google.com/'))"""
CLASSWORK_JS = """els => els.map(e => ({
  id: e.getAttribute('data-stream-item-id'), kind: e.innerText.trim().split('\\n')[0],
  title: e.querySelector('[role="button"][aria-expanded]')?.getAttribute('aria-label') || ''
}))"""
ASSIGNMENT_KINDS = {"Assignment", "Completed Assignment"}
DETAIL_JS = """id => {
  const heading = document.querySelector(`[data-stream-item-id="${id}"] h1`);
  const header = heading?.closest('[data-stream-item-id]');
  // The description is a sibling of the header, not inside its stream item.
  const description = header?.parentElement.querySelector(':scope > .nGi02b');
  return {
    description: description ? description.innerText.trim() : null,
    statuses: [...document.querySelectorAll('[data-submission-id] .u7S8tc')]
      .map(e => e.innerText.trim()),
    grade_labels: [...(header?.querySelectorAll('.W4hhKd') || [])]
      .map(e => e.innerText.trim())
  };
}"""


def apply_assignment_details(task: Task, detail: dict) -> None:
    """Use explicit visible values only; absent grade/description is not a reset."""
    available = set()
    if isinstance(detail.get("description"), str):
        task.description = detail["description"]
        available.add("description")
    normalized = {s.strip().lower() for s in detail.get("statuses", []) if s.strip()}
    if len(normalized) == 1:
        state = {
            "assigned": "open",
            "turned in": "submitted",
            "turned in late": "submitted",
            "missing": "missing",
            "returned": "returned",
            "done": "completed",
        }.get(normalized.pop())
        if state:
            task.submission_status = state
            available.add("submission_status")
    # Conflicting or unrecognized labels never erase a cached grade. A zero
    # numerator is a real grade; a points-only label says nothing about grading.
    grade_labels = {" ".join(label.split()) for label in detail.get("grade_labels", [])}
    grade_labels = {label for label in grade_labels if not label.lower().startswith("due ")}
    for label in grade_labels if len(grade_labels) == 1 else []:
        grade = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?:\s+points?)?", label, re.I)
        points = re.fullmatch(r"(\d+(?:\.\d+)?)\s+points?", label, re.I)
        if grade:
            task.score, task.points_possible = float(grade[1]), float(grade[2])
            task.graded = True
            available.update({"score", "points_possible", "graded"})
        elif points:
            task.points_possible = float(points[1])
            available.add("points_possible")
        elif label.lower() == "ungraded":
            task.score, task.points_possible, task.graded = None, None, False
            available.update({"score", "points_possible", "graded"})
    task.raw_data["unavailable_fields"] = [
        key for key in task.raw_data.get("unavailable_fields", []) if key not in available
    ]


def source_id(encoded: str) -> str:
    # Classroom's web URLs encode numeric API IDs. Refuse unknown encodings rather
    # than creating duplicates that could detach existing local notes.
    try:
        decoded = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
    except (ValueError, UnicodeError):
        raise ValueError("Unrecognized Classroom link ID") from None
    if not decoded.isdigit():
        raise ValueError("Unrecognized Classroom link ID")
    return decoded


def classroom_path(url: str) -> str:
    parsed = urlparse(urljoin(ORIGIN, url))
    return (
        parsed.path if parsed.scheme == "https" and parsed.netloc == "classroom.google.com" else ""
    )


def parse_courses(links: list[dict]) -> list[Course]:
    courses, priorities = {}, {}
    for link in links:
        match = COURSE_PATH.fullmatch(classroom_path(link["href"]))
        title = (
            (link.get("course_heading") or link.get("label") or link.get("text") or "")
            .strip()
            .splitlines()
        )
        priority = 3 if link.get("course_heading") else 2 if link.get("label") else 1
        if not match or not title:
            continue
        try:
            cid = source_id(match[1])
        except ValueError:
            continue
        if cid not in courses or priority > priorities[cid]:
            priorities[cid] = priority
            courses[cid] = Course(
                provider="google_classroom",
                external_id=cid,
                name=title[0],
                section=title[1] if len(title) > 1 else None,
                source_url=link["href"],
                raw_metadata={"transport": "browser"},
            )
    return list(courses.values())


def parse_task_links(links: list[dict], course_id: str) -> list[Task]:
    tasks = {}
    for link in links:
        match = TASK_PATH.fullmatch(classroom_path(link["href"]))
        title = (link.get("text") or link.get("label") or "").strip().splitlines()
        if not match or not title:
            continue
        try:
            cid, aid = source_id(match[1]), source_id(match[2])
        except ValueError:
            continue
        if cid != course_id:
            continue
        tasks.setdefault(
            aid,
            Task(
                provider="google_classroom",
                course_external_id=cid,
                external_id=aid,
                title=title[0],
                url=link["href"],
                raw_data={
                    "transport": "browser",
                    "unavailable_fields": [
                        "due_at",
                        "available_at",
                        "closes_at",
                        "description",
                        "submission_status",
                        "score",
                        "points_possible",
                        "graded",
                    ],
                },
            ),
        )
    return list(tasks.values())


def parse_classwork_items(items: list[dict], course: Course) -> list[Task]:
    links = []
    completed = set()
    for item in items:
        if item.get("kind") not in ASSIGNMENT_KINDS or not str(item.get("id", "")).isdigit():
            continue
        if item["kind"] == "Completed Assignment":
            completed.add(str(item["id"]))
        encoded = base64.b64encode(str(item["id"]).encode()).decode().rstrip("=")
        links.append(
            {"href": f"{course.source_url}/a/{encoded}/details", "text": item.get("title", "")}
        )
    tasks = parse_task_links(links, course.external_id)
    for task in tasks:
        if task.external_id in completed:
            task.submission_status = "completed"
            task.raw_data["unavailable_fields"].remove("submission_status")
    return tasks


class ClassroomBrowserConnector(BrowserConnector):
    key = "google_classroom"
    display_name = "Google Classroom"
    description = "Sign in with your school account in a dedicated browser. No Google Cloud setup."
    default_url = ORIGIN
    login_path = "/u/0/h"

    @property
    def configuration_fields(self):
        return [field for field in super().configuration_fields if field["key"] == "timezone"]

    @property
    def configuration_values(self):
        return {"timezone": self.config.get("timezone", self.db.settings().timezone)}

    @property
    def base_url(self):
        return ORIGIN

    async def configure(self, config):
        if config.keys() - {"timezone"}:
            raise ValueError("Unrecognized Classroom browser setting")
        await super().configure(config)

    async def validate_session(self, context):
        for page in context.pages:
            if not classroom_path(page.url):
                continue
            links = await page.evaluate(LINKS_JS)
            if parse_courses(links):
                return True
            # Empty accounts can be authenticated too. This link belongs to the
            # signed-in Google account menu, not the public Classroom landing page.
            if await page.locator('a[href*="accounts.google.com/SignOutOptions"]').count():
                return True
        return False

    async def finish_login(self):
        context = self.browser.interactive
        if context is not None:
            for page in context.pages:
                parsed = urlparse(page.url)
                if parsed.hostname == "accounts.google.com":
                    body = (await page.locator("body").inner_text()).lower()
                    if any(
                        s in body
                        for s in ["browser or app may not be secure", "此浏览器或应用可能不安全"]
                    ):
                        return {
                            "message": "Google rejected this browser. Login is not saved. "
                            "This browser connection is unavailable for this account."
                        }
        return await super().finish_login()

    async def read_page(self, page, url):
        if not classroom_path(url):
            raise ValueError("Classroom reader only accepts Classroom pages")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response and response.status == 429:
            raise TransportError(
                Outcome.RATE_LIMITED, "Google is rate limiting requests. Retry later."
            )
        if response and response.status >= 500:
            raise TransportError(Outcome.NETWORK_ERROR, "Classroom is temporarily unavailable.")
        if not classroom_path(page.url) or (response and response.status in {401, 403}):
            raise TransportError(Outcome.AUTH_REQUIRED, "Classroom session expired. Reconnect.")
        # Classroom renders asynchronously. Wait for content, never for networkidle
        # (Google pages retain background connections).
        path = classroom_path(url)
        selector = (
            "[data-stream-item-id] h1"
            if TASK_PATH.fullmatch(path)
            else "li[data-course-id]"
            if re.fullmatch(r"/(?:u/\d+/)?h/?", path)
            else "li[data-stream-item-id]"
            if "/w/" in path
            else "[data-stream-item-id]"
        )
        if re.fullmatch(r"/(?:u/\d+/)?h(?:/archived)?/?", path):
            await page.wait_for_function(
                """() =>
              !!document.querySelector('li[data-course-id]') ||
              /None of your classes have been archived|No classes|Join your first class/i
                .test(document.body.innerText)
            """,
                timeout=20000,
            )
        elif "/w/" in path:
            await page.wait_for_function(
                """() =>
              !!document.querySelector('li[data-stream-item-id]') ||
              /No classwork|This is where you'll see work/i.test(document.body.innerText)
            """,
                timeout=20000,
            )
        elif COURSE_PATH.fullmatch(path):
            await page.wait_for_function(
                """() => !!document.querySelector('[data-stream-item-id]') ||
                  [...document.querySelectorAll('a[href]')].some(a =>
                    /\\/w\\/[^/]+\\/t\\/all\\/?$/.test(new URL(a.href).pathname))""",
                timeout=20000,
            )
        else:
            await page.locator(selector).first.wait_for(timeout=20000)
        # A visible main landmark is only the loading shell. Wait for actual data
        # above, then for DOM changes to settle, with a bounded maximum.
        await page.evaluate("""() => new Promise(resolve => {
          let quiet, deadline;
          const done = () => { observer.disconnect(); clearTimeout(quiet);
            clearTimeout(deadline); resolve(); };
          const observer = new MutationObserver(() => {
            clearTimeout(quiet); quiet = setTimeout(done, 600);
          });
          observer.observe(document.body, {childList:true, subtree:true, characterData:true});
          quiet = setTimeout(done, 600); deadline = setTimeout(done, 4000);
        })""")
        return await page.evaluate(LINKS_JS)

    async def expand_list(self, page):
        """Load rendered continuations with a bound; never silently cut off a list."""
        previous, stable = None, 0
        for _ in range(40):
            expand = page.get_by_role("button", name=re.compile(r"^Expand topic$", re.I))
            for button in await expand.all():
                if await button.is_visible():
                    await button.click()
            more = page.get_by_role("button", name=re.compile(r"^(Show|Load) more$", re.I))
            clicked = False
            for button in await more.all():
                if await button.is_visible():
                    await button.click()
                    clicked = True
                    break
            shape = await page.evaluate("""() => {
              const root = document.scrollingElement;
              root.scrollTop = root.scrollHeight;
              for (const el of document.querySelectorAll('main, [role="main"]')) {
                el.scrollTop = el.scrollHeight;
              }
              const count = document.querySelectorAll(
                'li[data-stream-item-id], li[data-course-id]').length;
              return [count, root.scrollHeight];
            }""")
            await page.wait_for_timeout(700)
            stable = stable + 1 if shape == previous and not clicked else 0
            previous = shape
            if stable >= 3:
                return
        raise TransportError(
            Outcome.PARTIAL, "Classroom list did not finish loading; saved tasks retained."
        )

    async def read_course_list(self, page, url, *, archived=False):
        await self.read_page(page, url)
        await self.expand_list(page)
        # Sidebar links include active courses even on an empty archived page.
        links = await page.locator("li[data-course-id] a[href]").evaluate_all(
            """els => els.map(a => ({href:a.href, text:a.innerText,
              course_heading:a.closest('li[data-course-id]')
                ?.querySelector('h2')?.innerText || ''}))"""
        )
        courses = parse_courses(links)
        for course in courses:
            course.raw_metadata["archived"] = archived
        return courses

    async def read_assignment(self, page, task):
        pending, timings = [], []
        timing_ready = asyncio.Event()
        heading_ready = asyncio.Event()

        async def capture(response):
            parsed = urlparse(response.url)
            if (
                parsed.hostname != "classroom.google.com"
                or response.status != 200
                or not parsed.path.endswith("/batchexecute")
            ):
                return
            try:
                body = await response.text()
                # Stream reminders can prefix a due time to the title. Validate
                # against the assignment's own heading, even if its response wins
                # the race against rendering that heading.
                await heading_ready.wait()
                timing = assignment_timing(
                    body, task.course_external_id, task.external_id, task.title
                )
                if timing:
                    timings.append(timing)
                    timing_ready.set()
            except BrowserError:
                pass

        def received(response):
            pending.append(asyncio.create_task(capture(response)))

        page.on("response", received)
        try:
            await self.read_page(page, task.url)
            heading = page.locator(f'[data-stream-item-id="{task.external_id}"] h1')
            title = (await heading.inner_text()).strip()
            if title:
                task.title = title
            heading_ready.set()
            # The visible heading can settle before Classroom sends its timing
            # request. A snapshot of pending jobs alone misses that later response.
            if not timings:
                try:
                    await asyncio.wait_for(timing_ready.wait(), timeout=3)
                except TimeoutError:
                    pass
            await asyncio.gather(*pending)
            if timings:
                task.due_at = timings[-1]["due_at"]
                task.source_updated_at = timings[-1]["source_updated_at"]
                task.raw_data["unavailable_fields"].remove("due_at")
            apply_assignment_details(task, await page.evaluate(DETAIL_JS, task.external_id))
        finally:
            page.remove_listener("response", received)
            for job in pending:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def read_course(self, page, course, result):
        links = await self.read_page(page, course.source_url)
        tasks = {t.id: t for t in parse_task_links(links, course.external_id)}
        result.tasks.extend(tasks.values())
        encoded = COURSE_PATH.fullmatch(classroom_path(course.source_url))[1]
        classwork = next(
            (
                link["href"]
                for link in links
                if re.fullmatch(
                    rf"/(?:u/\d+/)?w/{re.escape(encoded)}/t/all/?",
                    classroom_path(link["href"]),
                )
            ),
            None,
        )
        if classwork:
            await self.read_page(page, classwork)
            await self.expand_list(page)
            items = await page.locator("li[data-stream-item-id]").evaluate_all(CLASSWORK_JS)
            for task in parse_classwork_items(items, course):
                if task.id not in tasks:
                    tasks[task.id] = task
                    result.tasks.append(task)
                else:
                    # Classwork titles omit the due-time prefixes found in stream
                    # reminder links. Keep the already retained Task object.
                    tasks[task.id].title = task.title
                    tasks[task.id].url = task.url
                    if task.submission_status != "unknown":
                        tasks[task.id].submission_status = task.submission_status
                        tasks[task.id].raw_data["unavailable_fields"] = [
                            field
                            for field in tasks[task.id].raw_data["unavailable_fields"]
                            if field != "submission_status"
                        ]
            unsupported = sum(
                item.get("kind") not in ASSIGNMENT_KINDS | {"Material"} for item in items
            )
            if unsupported:
                result.warnings.append(
                    f"{course.name}: {unsupported} question or unrecognized "
                    "classwork items "
                    "could not be imported; check Classwork."
                )
            result.metadata.setdefault("classwork_items", {})[course.external_id] = len(items)
        else:
            result.warnings.append(
                f"{course.name}: Classwork link not found; only stream assignments read."
            )
        for task in tasks.values():
            try:
                await self.read_assignment(page, task)
            except (BrowserError, TransportError) as exc:
                if isinstance(exc, TransportError) and exc.outcome == Outcome.AUTH_REQUIRED:
                    raise
                result.warnings.append(
                    "An assignment detail could not be refreshed; known fields retained."
                )

    async def sync(self):
        if self.connection_status() != "connected":
            return SyncResult(
                outcome=Outcome.AUTH_REQUIRED, warnings=["Sign in to Classroom first."]
            )
        result = SyncResult(
            outcome=Outcome.PARTIAL,
            complete=False,
            metadata={
                "transport": "browser",
                "coverage": "visible_classwork",
                "dates": "page_responses",
            },
        )
        try:
            async with self.browser.session(self.configuration_values["timezone"]) as context:
                page = await context.new_page()
                result.courses = await self.read_course_list(page, ORIGIN + self.login_path)
                try:
                    archived = await self.read_course_list(
                        page, ORIGIN + "/u/0/h/archived", archived=True
                    )
                    known = {course.id for course in result.courses}
                    result.courses.extend(course for course in archived if course.id not in known)
                    result.metadata["archived_courses_checked"] = True
                    result.metadata["archived_courses"] = len(archived)
                except (BrowserError, TransportError) as exc:
                    if isinstance(exc, TransportError) and exc.outcome == Outcome.AUTH_REQUIRED:
                        raise
                    result.warnings.append("Archived Classroom courses could not be refreshed.")
                if not result.courses:
                    raise TransportError(
                        Outcome.PARSE_ERROR,
                        "No course links recognized. Empty account or changed page; "
                        "saved tasks retained.",
                    )
                for course in result.courses:
                    try:
                        await self.read_course(page, course, result)
                    except (BrowserError, TransportError) as exc:
                        if isinstance(exc, TransportError) and exc.outcome == Outcome.AUTH_REQUIRED:
                            raise
                        result.warnings.append(
                            f"{course.name}: could not finish refreshing; saved tasks retained."
                        )
                missing = sum(
                    bool(
                        set(task.raw_data["unavailable_fields"])
                        & {
                            "description",
                            "due_at",
                            "submission_status",
                            "score",
                            "points_possible",
                            "graded",
                        }
                    )
                    for task in result.tasks
                )
                if missing:
                    unavailable = sorted(
                        {
                            field
                            for task in result.tasks
                            for field in task.raw_data["unavailable_fields"]
                            if field
                            in {
                                "description",
                                "due_at",
                                "submission_status",
                                "score",
                                "points_possible",
                                "graded",
                            }
                        }
                    )
                    result.metadata["unavailable_assignment_fields"] = unavailable
                    labels = sorted(
                        {
                            {
                                "description": "instructions",
                                "due_at": "deadline",
                                "submission_status": "submission status",
                                "score": "grade and points",
                                "points_possible": "grade and points",
                                "graded": "grade and points",
                            }[field]
                            for field in unavailable
                        }
                    )
                    result.warnings.append(
                        f"{missing} Classroom assignment(s): "
                        + ", ".join(labels)
                        + " not exposed or recognized on the page; cached values retained."
                    )
        except TransportError as exc:
            result.outcome = Outcome.PARTIAL if result.courses else exc.outcome
            result.warnings.append(exc.safe_message)
            if exc.outcome == Outcome.AUTH_REQUIRED:
                self.db.update_state(self.key, authorized=False)
        except BrowserError:
            result.outcome = Outcome.PARTIAL if result.courses else Outcome.PARSE_ERROR
            result.warnings.append(
                "Classroom page could not be read. Reopen login to check the session; "
                "saved tasks retained."
            )
        return result
