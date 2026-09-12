"""Collect Classroom materials as knowledge, without creating or changing tasks.

The caller owns the source lock and persistent browser session. Only ordinary
Classroom material pages are opened; linked files are retained as references.
"""

import base64
import re
from urllib.parse import parse_qsl, urlparse, urlunparse

from playwright.async_api import Error as BrowserError

from ..domain import Course, Outcome, now
from .classroom_browser import CLASSWORK_JS, COURSE_PATH, ORIGIN, classroom_path, source_id
from .http import TransportError
from .material_retry import retry_material_read

MATERIAL_PATH = re.compile(r"/(?:u/\d+/)?c/([A-Za-z0-9_-]+)/m/([A-Za-z0-9_-]+)/details/?")
MATERIAL_DETAIL_JS = """id => {
  const heading = document.querySelector(`[data-stream-item-id="${id}"] h1`);
  const root = heading?.closest('[data-stream-item-id]')?.parentElement;
  const description = root?.querySelector(':scope > .nGi02b');
  const scope = root?.parentElement?.classList.contains('EE538') ? root.parentElement : root;
  return {
    title: heading?.innerText.trim() || '',
    body: description ? description.innerText.trim() : null,
    links: scope ? [...scope.querySelectorAll('a[href]')]
      .filter(a => !a.closest('.PeGHgb')).map(a => ({
      url: a.href, title: a.innerText.trim() || a.getAttribute('aria-label') || ''
    })) : []
  };
}"""
_SECRET_QUERY = re.compile(
    r"token|auth|pass|secret|session|ticket|signature|credential|challenge|(?:^|_)key$", re.I
)
_URL = re.compile(r"https?://[^\s<>\"']+")
ANNOUNCEMENTS_JS = """() => [...document.querySelectorAll(
  'div.qhnNic[data-include-stream-item-materials][data-stream-item-id]:not(.xo7QFd)'
)].map(e => ({id:e.getAttribute('data-stream-item-id'),
  body:e.querySelector('.n4xnA .n8F6Jd .pco8Kc')?.innerText.trim() ?? null
}))"""


async def read_google_doc_text(context, url: str) -> str | None:
    """Use the document's own File > Download action, never a guessed API URL."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/document/d/([A-Za-z0-9_-]+)/(?:edit|view|preview)", parsed.path)
    if parsed.hostname != "docs.google.com" or not match:
        return None
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.get_by_role("menuitem", name="File", exact=True).click(timeout=8000)
        await page.get_by_role("menuitem", name="Download", exact=False).hover(timeout=5000)
        async with page.expect_download(timeout=10000) as pending:
            await page.get_by_role("menuitem", name="Plain Text (.txt)", exact=False).click(
                timeout=5000
            )
        download = await pending.value
        target = urlparse(download.url)
        parameters = dict(parse_qsl(target.query))
        # Authentication-bearing download URLs remain transient in this method.
        direct_export = target.hostname == "docs.google.com" and target.path.endswith("/export")
        generated_text = bool(
            re.fullmatch(r"doc-[a-z0-9-]+-docstext\.googleusercontent\.com", target.hostname or "")
        )
        if (
            target.scheme != "https"
            or not (direct_export or generated_text)
            or not (f"/d/{match[1]}/" in target.path or parameters.get("id") == match[1])
            or parameters.get("format") != "txt"
        ):
            return None
        response = await context.request.get(download.url, timeout=20000)
        if response.status != 200 or "text/plain" not in response.headers.get("content-type", ""):
            return None
        content = await response.text()
        return content.lstrip("\ufeff") if len(content) <= 2_000_000 else None
    except BrowserError:
        return None
    finally:
        await page.close()


def reference_url(value: str) -> str | None:
    """Do not let browser authentication parameters enter the knowledge store."""
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        query = parse_qsl(parsed.query, keep_blank_values=True)
        google_doc = parsed.hostname in {"docs.google.com", "drive.google.com"}
        if any(
            _SECRET_QUERY.search(key) and not (google_doc and key == "authuser" and value.isdigit())
            for key, value in query
        ):
            return None
        if google_doc:
            # Sharing and editor display options are not part of document identity.
            if any(
                key not in {"usp", "tab", "embedded", "rm", "hl", "authuser"}
                for key, _value in query
            ):
                return None
            return urlunparse(parsed._replace(query="", fragment=""))
        return value
    except ValueError:
        return None


def material_document(
    course: Course, external_id: str, detail: dict, url: str
) -> tuple[dict, bool]:
    match = MATERIAL_PATH.fullmatch(classroom_path(url))
    if (
        course.provider != "google_classroom"
        or not match
        or source_id(match[1]) != course.external_id
        or source_id(match[2]) != external_id
        or not detail.get("title")
    ):
        raise ValueError("Material page identity could not be verified")
    skipped = False

    def clean_body_link(match):
        nonlocal skipped
        safe = reference_url(match[0])
        if safe is None:
            skipped = True
        return safe or "[Link available on the source page]"

    body = _URL.sub(clean_body_link, detail.get("body") or "").strip()
    references = {}
    collected = detail.get("linked_text", {})
    for link in detail.get("links", []):
        raw = str(link.get("url") or "")
        safe = reference_url(raw)
        if safe is None:
            skipped = True
            continue
        if urlparse(safe).hostname == "classroom.google.com":
            continue
        references[safe] = _URL.sub(clean_body_link, str(link.get("title") or safe)).strip()
    if references:
        for href, title in list(references.items()):
            if isinstance(collected.get(href), str) and collected[href].strip():
                body += ("\n\n" if body else "") + f"Attached document: {title}\n"
                body += _URL.sub(clean_body_link, collected[href])
                del references[href]
        if references:
            body += ("\n\n" if body else "") + "Linked documents (contents not collected):\n"
            body += "\n".join(f"{title}: {href}" for href, title in references.items())
    if detail.get("body") is None and not body:
        raise ValueError("Material body could not be recognized")
    warnings = []
    if references:
        warnings.append("Linked file contents have not been read.")
    if skipped:
        warnings.append("Some links require opening the source page.")
    stamp = now().isoformat()
    return {
        "id": f"google_classroom:{course.external_id}:material:{external_id}",
        "provider": "google_classroom",
        "course_id": course.id,
        "title": detail["title"].strip(),
        "body": body,
        "url": ORIGIN + classroom_path(url),
        "kind": "material",
        "updated_at": None,
        "source_modified_at": None,
        "fetched_at": stamp,
        "checked_at": stamp,
        "complete": not warnings,
        "warnings": warnings,
        "evidence": "classroom_material_page",
        "source_task_id": None,
    }, bool(references) or skipped


async def collect_classroom_materials(
    connector,
    page,
    course: Course,
    on_document=None,
    *,
    resume_identity=None,
    on_checkpoint=None,
    max_body_reads=None,
) -> dict:
    """Return materials read in this run; callers retain cached rows on failures."""
    result = {"documents": [], "warnings": [], "complete": False}
    match = COURSE_PATH.fullmatch(classroom_path(course.source_url or ""))
    try:
        valid_course = bool(match) and source_id(match[1]) == course.external_id
    except ValueError:
        valid_course = False
    if course.provider != "google_classroom" or not valid_course:
        result["warnings"].append("Classroom course link could not be verified.")
        return result
    links_only = False
    export_count = 0

    async def read_page(url, stage):
        # Only explicit network/rate failures retry; selector/permission failures remain visible.
        return await retry_material_read(
            lambda: connector.read_page(page, url),
            errors=result.setdefault("errors", []),
            scope=course.id,
            stage=stage,
        )

    try:
        # Navigating the real course first lets the existing profile restore SSO.
        await read_page(course.source_url, "announcement_index")
        await connector.expand_list(page)
        posts = await page.evaluate(ANNOUNCEMENTS_JS)
        for post in posts:
            if not str(post.get("id", "")).isdigit():
                result["warnings"].append("A Classroom announcement could not be read.")
                continue
            read = isinstance(post.get("body"), str)
            removed_links = False

            def clean_announcement_link(match):
                nonlocal removed_links
                safe = reference_url(match[0])
                removed_links |= safe is None
                return safe or "[Link available on source page]"

            body = _URL.sub(clean_announcement_link, post["body"] if read else "")
            warnings = (
                [] if read else ["Announcement body could not be read; cached copy retained."]
            )
            if removed_links:
                warnings.append("Some links require opening the source page.")
            stamp = now().isoformat()
            result["documents"].append(
                {
                    "id": f"google_classroom:{course.external_id}:announcement:{post['id']}",
                    "provider": "google_classroom",
                    "course_id": course.id,
                    "title": body.splitlines()[0][:200] if body else "Announcement",
                    "body": body,
                    "url": ORIGIN + classroom_path(course.source_url),
                    "kind": "announcement",
                    "updated_at": None,
                    "source_modified_at": None,
                    "fetched_at": stamp if read else None,
                    "checked_at": stamp,
                    "complete": not warnings,
                    "warnings": warnings,
                    "evidence": "classroom_stream" if read else "classroom_stream_index",
                    "source_task_id": None,
                }
            )
            if on_document is not None:
                on_document(result["documents"][-1])
            result["warnings"].extend(warnings)
        classwork = f"{ORIGIN}/u/0/w/{match[1]}/t/all"
        await read_page(classwork, "material_index")
        await connector.expand_list(page)
        items = await page.locator("li[data-stream-item-id]").evaluate_all(CLASSWORK_JS)
        materials = [item for item in items if item.get("kind") == "Material"]
        result["complete"] = not result["warnings"]
        indexed = []
        for item in materials:
            external_id = str(item.get("id") or "")
            if not external_id.isdigit():
                result["complete"] = False
                result["warnings"].append("A Classroom material identifier was not recognized.")
                continue
            encoded = base64.b64encode(external_id.encode()).decode().rstrip("=")
            url = f"{ORIGIN}{classroom_path(course.source_url).rstrip('/')}/m/{encoded}/details"
            document = {
                "id": f"google_classroom:{course.external_id}:material:{external_id}",
                "provider": "google_classroom",
                "course_id": course.id,
                "title": str(item.get("title") or "Material"),
                "body": "",
                "url": url,
                "kind": "material",
                "updated_at": None,
                "source_modified_at": None,
                "fetched_at": None,
                "checked_at": now().isoformat(),
                "complete": False,
                "warnings": ["Material details have not been read."],
                "evidence": "classroom_material_index",
                "source_task_id": None,
            }
            result["documents"].append(document)
            if on_document is not None:
                on_document(document)
            indexed.append((external_id, url, document))
        if resume_identity:
            position = next(
                (index for index, item in enumerate(indexed) if item[0] == resume_identity), 0
            )
            indexed = indexed[position:] + indexed[:position]
        for position, (external_id, url, indexed_document) in enumerate(indexed):
            if on_checkpoint is not None:
                on_checkpoint(external_id)
            if max_body_reads is not None and position >= max_body_reads:
                result["complete"] = False
                result["warnings"].append(
                    f"{len(indexed) - position} Classroom material bodies "
                    "await the next reading pass."
                )
                break
            try:
                await read_page(url, "material_detail")
                await page.locator(f'[data-stream-item-id="{external_id}"] h1').wait_for(
                    timeout=15000
                )
                detail = await page.evaluate(MATERIAL_DETAIL_JS, external_id)
                detail["linked_text"] = {}
                for link in detail.get("links", []):
                    safe = reference_url(str(link.get("url") or ""))
                    if not safe or export_count >= 5:
                        continue
                    parsed_link = urlparse(safe)
                    if (
                        parsed_link.hostname == "docs.google.com"
                        and "/document/d/" in parsed_link.path
                    ):
                        export_count += 1
                        text = await read_google_doc_text(page.context, safe)
                        if text:
                            detail["linked_text"][safe] = text
                document, partial = material_document(course, external_id, detail, url)
                indexed_document.clear()
                indexed_document.update(document)
                if on_document is not None:
                    on_document(indexed_document)
                links_only |= partial
            except (BrowserError, TransportError, ValueError) as exc:
                indexed_document["warnings"] = [
                    "Material details could not be read; cached copy retained."
                ]
                indexed_document["checked_at"] = now().isoformat()
                if on_document is not None:
                    on_document(indexed_document)
                if isinstance(exc, TransportError) and exc.outcome == Outcome.AUTH_REQUIRED:
                    raise
                if not isinstance(exc, TransportError):
                    result.setdefault("errors", []).append(
                        {
                            "category": "read_error",
                            "scope": course.id,
                            "stage": "material_detail",
                            "retry_attempt": 0,
                        }
                    )
                result["complete"] = False
                result["warnings"].append(
                    "A Classroom material could not be read; cached copy retained."
                )
        if links_only:
            result["complete"] = False
            result["warnings"].append(
                "Linked file contents are not yet included in Classroom materials."
            )
    except (BrowserError, TransportError) as exc:
        if not isinstance(exc, TransportError):
            result.setdefault("errors", []).append(
                {
                    "category": "read_error",
                    "scope": course.id,
                    "stage": "material_index",
                    "retry_attempt": 0,
                }
            )
        result["complete"] = False
        result["warnings"].append(
            exc.safe_message
            if isinstance(exc, TransportError)
            else "Classroom materials could not be refreshed; cached copies retained."
        )
    return result
