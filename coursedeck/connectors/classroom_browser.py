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
    for item in items:
        if item.get("kind") != "Assignment" or not str(item.get("id", "")).isdigit():
            continue
        encoded = base64.b64encode(str(item["id"]).encode()).decode().rstrip("=")
        links.append(
            {"href": f"{course.source_url}/a/{encoded}/details", "text": item.get("title", "")}
        )
    return parse_task_links(links, course.external_id)


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

    async def read_assignment(self, page, task):
        pending, timings = [], []

        async def capture(response):
            parsed = urlparse(response.url)
            if (
                parsed.hostname != "classroom.google.com"
                or response.status != 200
                or not parsed.path.endswith("/batchexecute")
            ):
                return
            try:
                timing = assignment_timing(
                    await response.text(), task.course_external_id, task.external_id, task.title
                )
                if timing:
                    timings.append(timing)
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
            await asyncio.gather(*pending)
            if timings:
                task.due_at = timings[-1]["due_at"]
                task.source_updated_at = timings[-1]["source_updated_at"]
                task.raw_data["unavailable_fields"].remove("due_at")
            statuses = await page.locator("[data-submission-id] .u7S8tc").all_text_contents()
            normalized = {s.strip().lower() for s in statuses if s.strip()}
            if len(normalized) == 1:
                state = {
                    "assigned": "open",
                    "turned in": "submitted",
                    "missing": "missing",
                    "returned": "returned",
                }.get(normalized.pop())
                if state:
                    task.submission_status = state
                    task.raw_data["unavailable_fields"].remove("submission_status")
        finally:
            page.remove_listener("response", received)
            for job in pending:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

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
                links = await self.read_page(page, ORIGIN + self.login_path)
                result.courses = parse_courses(links)
                if not result.courses:
                    raise TransportError(
                        Outcome.PARSE_ERROR,
                        "No course links recognized. Empty account or changed page; "
                        "saved tasks retained.",
                    )
                for course in result.courses:
                    links = await self.read_page(page, course.source_url)
                    tasks = {t.id: t for t in parse_task_links(links, course.external_id)}
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
                        items = await page.locator("li[data-stream-item-id]").evaluate_all(
                            CLASSWORK_JS
                        )
                        tasks.update({t.id: t for t in parse_classwork_items(items, course)})
                    result.tasks.extend(tasks.values())
                    for task in tasks.values():
                        try:
                            await self.read_assignment(page, task)
                        except BrowserError:
                            result.warnings.append(
                                "An assignment detail could not be refreshed; "
                                "known fields retained."
                            )
                result.warnings.append(
                    "Synced visible assignments. "
                    "Archived courses, questions and grades are not yet covered."
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
