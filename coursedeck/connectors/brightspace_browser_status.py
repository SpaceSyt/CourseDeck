"""Read status labels from the same student pages the user can see."""

import re
from collections import Counter
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError

from ..domain import Course, Outcome
from .http import TransportError


def normalized(text):
    return " ".join(text.split())


def folder_statuses(html: str, course: Course, folders: list[dict]) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for name in soup.select(".d2l-foldername"):
        if name.find_parent(class_="d2l-htmlblock-untrusted"):
            continue
        row = name.find_parent("tr")
        if row:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) >= 2:
                rows.append((name, cells))
    counts = Counter(normalized(name.get_text(" ", strip=True)) for name, _ in rows)
    by_name = {}
    for folder in folders:
        by_name.setdefault(normalized(folder["Name"]), []).append(str(folder["Id"]))
    allowed_ids = {str(folder["Id"]) for folder in folders}
    result = {}
    for name, cells in rows:
        title = normalized(name.get_text(" ", strip=True))
        ids = set()
        for link in name.select("a[href]"):
            url = urlparse(urljoin(course.source_url, str(link["href"])))
            params = parse_qs(url.query)
            if url.netloc != urlparse(course.source_url).netloc:
                continue
            if params.get("ou", [course.external_id]) != [course.external_id]:
                continue
            ids.update(i for i in params.get("db", []) if i in allowed_ids)
        # Restricted assignments have plain labels. Enrich an existing API identity only
        # when the full title is unique in both lists; never create an identity from a title.
        if not ids and not name.select_one("a[href]") and counts[title] == 1:
            candidates = by_name.get(title, [])
            if len(candidates) == 1:
                ids.add(candidates[0])
        if len(ids) != 1:
            continue
        label = normalized(cells[1].get_text(" ", strip=True)).lower()
        status = {
            "not submitted": "open",
            "not complete": "open",
            "incomplete": "open",
            "submitted": "submitted",
            "complete": "completed",
            "completed": "completed",
        }.get(label)
        if status is None and re.fullmatch(r"[1-9]\d* (?:submission|file)s?", label):
            status = "submitted"
        if status:
            key = next(iter(ids))
            value = {"status": status, "evidence": "student_assignment_list"}
            if key in result and result[key] != value:
                result[key] = {"status": "unknown", "evidence": "conflicting_rows"}
            else:
                result[key] = value
    return result


class BrightspaceBrowserStatus:
    def __init__(self, context, base_url):
        self.context, self.base_url = context, base_url

    def check_page(self, page, path, course_id):
        target, base = urlparse(page.url), urlparse(self.base_url)
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or target.path != path
            or parse_qs(target.query).get("ou") != [course_id]
        ):
            raise TransportError(Outcome.PARSE_ERROR, "Student status page identity did not match.")

    async def folders(self, course, folders):
        if not folders:
            return {}
        page = await self.context.new_page()
        try:
            await page.goto(
                f"{self.base_url}/d2l/lms/dropbox/user/folders_list.d2l?ou={course.external_id}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.locator(".d2l-foldername").first.wait_for(timeout=15000)
            self.check_page(page, "/d2l/lms/dropbox/user/folders_list.d2l", course.external_id)
            # Use the largest supported page size, then visit remaining pages if offered.
            size = page.locator("select").filter(has=page.locator('option[value="200"]')).first
            if await size.count() and await size.input_value() != "200":
                await size.select_option("200")
                await page.wait_for_load_state("domcontentloaded")
                await page.locator(".d2l-foldername").first.wait_for(timeout=15000)
            result, seen = {}, set()
            for _ in range(100):
                self.check_page(page, "/d2l/lms/dropbox/user/folders_list.d2l", course.external_id)
                html = await page.content()
                current = folder_statuses(html, course, folders)
                identity = tuple(await page.locator(".d2l-foldername").all_text_contents())
                if identity in seen:
                    raise TransportError(
                        Outcome.PARTIAL, "Assignment status pagination did not advance."
                    )
                seen.add(identity)
                for key, value in current.items():
                    if key in result and result[key] != value:
                        result[key] = {"status": "unknown", "evidence": "conflicting_rows"}
                    else:
                        result[key] = value
                next_page = page.get_by_role("button", name=re.compile(r"^next page$", re.I))
                if not await next_page.count() or not await next_page.first.is_enabled():
                    break
                await next_page.first.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.locator(".d2l-foldername").first.wait_for(timeout=15000)
            return result
        except BrowserError as exc:
            raise TransportError(
                Outcome.PARTIAL, "Assignment status page could not be read."
            ) from exc
        finally:
            await page.close()

    async def quiz(self, course, raw):
        from .brightspace_activities import parse_quiz_summary

        page = await self.context.new_page()
        try:
            await page.goto(
                f"{self.base_url}/d2l/lms/quizzing/user/quiz_summary.d2l?qi={raw['QuizId']}&ou={course.external_id}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.get_by_text("Quiz Details", exact=True).wait_for(timeout=15000)
            self.check_page(page, "/d2l/lms/quizzing/user/quiz_summary.d2l", course.external_id)
            query = parse_qs(urlparse(page.url).query)
            title = normalized(await page.locator("h1#d_page_title").inner_text())
            if (
                urlparse(page.url).netloc != urlparse(self.base_url).netloc
                or query.get("qi") != [str(raw["QuizId"])]
                or query.get("ou") != [course.external_id]
                or title != "Summary - " + normalized(raw["Name"])
            ):
                raise TransportError(Outcome.PARSE_ERROR, "Quiz summary identity did not match.")
            # The quiz summary is read-only. Never click Start Quiz or Retake.
            return parse_quiz_summary(await page.content())
        except BrowserError as exc:
            raise TransportError(Outcome.PARTIAL, "Quiz summary could not be read.") from exc
        finally:
            await page.close()
