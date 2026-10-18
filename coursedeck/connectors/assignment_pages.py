"""Read explicit assignment-list pagination without guessing navigation actions."""

from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qsl, urljoin, urlparse

from bs4 import BeautifulSoup

MAX_ASSIGNMENT_PAGES = 100
PAGERS = '.pagination, .dataTables_paginate, [aria-label*="pagination" i]'
NEXT = 'a[rel="next"], [aria-label="next page" i], .next'


def pagination_step(html: str, current_url: str):
    """Return HTML without verified controls, next GET URL, and a coverage issue."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(
        'input[type="search"][value]:not([value=""]), [data-filtered="true"], '
        '[data-has-more="true"], [aria-busy="true"]'
    ):
        return html, None, "Assignment list is filtered, still loading, or truncated."
    pagers = soup.select(PAGERS)
    controls = soup.select(NEXT)
    for pager in pagers:
        controls.extend(
            node
            for node in pager.select("a, button, span")
            if node.get_text(" ", strip=True).lower() in {"next", "next page", "next ›", "next »"}
        )
    # A wrapper and its nested link describe one control.
    controls = list({id(node): node for node in controls}.values())
    identities = {id(node) for node in controls}
    controls = [
        node for node in controls if not any(id(parent) in identities for parent in node.parents)
    ]
    if not pagers and not controls:
        return html, None, None
    if len(controls) != 1:
        return html, None, "Pagination controls are unrecognized or ambiguous."
    control = controls[0]
    disabled = any(
        node.has_attr("disabled")
        or node.get("aria-disabled") == "true"
        or "disabled" in node.get("class", [])
        for node in [control, *control.parents]
        if getattr(node, "attrs", None) is not None
    )
    link = control if control.name == "a" else control.find("a")
    current = urlparse(current_url)
    parameters = parse_qsl(current.query, keep_blank_values=True)
    pages = [value for key, value in parameters if key == "page"]
    if len(pages) > 1 or (pages and (not pages[0].isdigit() or int(pages[0]) < 1)):
        return html, None, "Pagination page identity could not be verified."
    page_number = int(pages[0]) if pages else 1
    for pager in pagers:
        for active in pager.select('[aria-current="page"], .active'):
            label = active.get_text(" ", strip=True)
            if label.isdecimal() and int(label) != page_number:
                return html, None, "Pagination page identity conflicts with the visible page."
    next_url = None
    if not disabled:
        if (
            link is None
            or not link.get("href")
            or any(link.has_attr(key) for key in ("onclick", "data-method", "download"))
        ):
            return html, None, "Pagination requires an unsupported navigation control."
        next_url = urljoin(current_url, str(link["href"]))
        target = urlparse(next_url)
        target_parameters = parse_qsl(target.query, keep_blank_values=True)
        target_pages = [value for key, value in target_parameters if key == "page"]
        if (
            target.scheme != "https"
            or target.netloc != current.netloc
            or target.path != current.path
            or target.username
            or target.password
            or target.fragment
            or target_pages != [str(page_number + 1)]
            or sorted((k, v) for k, v in target_parameters if k != "page")
            != sorted((k, v) for k, v in parameters if k != "page")
        ):
            return html, None, "Pagination link changes course, filters, or page order."
    for node in [*controls, *pagers]:
        if node.parent is not None:
            node.decompose()
    return str(soup), next_url, None


async def assignment_pages(
    page,
    html: str,
    wait_ready: Callable[[], Awaitable[None]],
) -> AsyncIterator[tuple[str, bool, str | None]]:
    """Yield retained pages immediately; only verified exhaustion authorizes coverage."""
    initial = urlparse(page.url)
    initial_pages = [
        value for key, value in parse_qsl(initial.query, keep_blank_values=True) if key == "page"
    ]
    if initial_pages and initial_pages != ["1"]:
        yield html, False, "Assignment reading did not start on the first page."
        return
    seen = set()
    paginated = False
    for index in range(MAX_ASSIGNMENT_PAGES):
        cleaned, next_url, issue = pagination_step(html, page.url)
        # Ignore pagination labels: a changing page counter alone is not new content.
        signature = BeautifulSoup(cleaned, "html.parser")
        rows = tuple(str(row) for row in signature.select("tbody tr"))
        if rows in seen:
            yield (
                cleaned,
                False,
                "Assignment pagination repeated a page; remaining pages were not read.",
            )
            return
        seen.add(rows)
        if paginated and not issue and not next_url and cleaned == html:
            issue = "Assignment pagination ended without a verified terminal control."
        if next_url and index + 1 == MAX_ASSIGNMENT_PAGES:
            issue = "Assignment pagination reading limit reached."
        yield cleaned, not issue and next_url is None, issue
        if issue or next_url is None:
            return
        paginated = True
        response = await page.goto(next_url, wait_until="domcontentloaded", timeout=45000)
        # A redirect can silently switch course or return to the first page.
        if page.url != next_url or (response and response.status >= 400):
            yield (
                "",
                False,
                "An assignment page was unavailable or redirected; cached tasks retained.",
            )
            return
        await wait_ready()
        html = await page.content()
