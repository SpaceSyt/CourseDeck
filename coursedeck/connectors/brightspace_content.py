"""Enrich existing dated content; never infer new tasks or dates from prose."""

import asyncio
import io
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .http import TransportError

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARACTERS = 400_000
MAX_PDF_PAGES = 100


def body_warning(result, course, title, reason):
    issues = result.metadata.setdefault("content_body_issues", [])
    issue = next(
        (
            item
            for item in issues
            if item["course_id"] == course.external_id and item["reason"] == reason
        ),
        None,
    )

    def message(item):
        return f"{course.name}: {reason} — {', '.join(item['titles'])}"

    if issue is None:
        issue = {"course_id": course.external_id, "reason": reason, "titles": []}
        issues.append(issue)
    else:
        previous = message(issue)
        if previous in result.warnings:
            result.warnings.remove(previous)
    if title not in issue["titles"]:
        issue["titles"].append(title)
    result.warnings.append(message(issue))


def rich_text(raw: dict) -> str:
    if not isinstance(raw, dict) or not any(field in raw for field in ("Text", "Html")):
        raise ValueError("Content description schema missing")
    if raw.get("Text") is not None:
        if not isinstance(raw["Text"], str):
            raise ValueError("Content text was invalid")
        if raw["Text"].strip():
            return raw["Text"].strip()
    if raw.get("Html") is not None:
        if not isinstance(raw["Html"], str):
            raise ValueError("Content HTML was invalid")
        return html_text(raw["Html"])
    return ""


def html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,template,noscript"):
        node.decompose()
    return soup.get_text("\n", strip=True)


def static_file_path(raw_url: str, base_url: str, course_id: str) -> str | None:
    """Only uncredentialed same-course static content, not LMS views or LTI links."""
    if not isinstance(raw_url, str):
        return None
    target, base = urlparse(urljoin(base_url, raw_url)), urlparse(base_url)
    decoded = unquote(target.path)
    prefix = f"/content/enforced/{course_id}"
    if (
        target.scheme != base.scheme
        or target.netloc != base.netloc
        or target.username
        or target.password
        or target.query
        or target.fragment
        or not (decoded.startswith(prefix + "-") or decoded.startswith(prefix + "/"))
        or ".." in PurePosixPath(decoded).parts
        or "\\" in decoded
    ):
        return None
    return target.path


def document_text(body: bytes, content_type: str) -> tuple[str, list[str]]:
    if len(body) > MAX_FILE_BYTES:
        raise ValueError("Content file exceeds the local reading limit")
    mime = content_type.split(";", 1)[0].strip().lower()
    warnings = []
    if mime == "application/pdf":
        if not body.startswith(b"%PDF-"):
            raise ValueError("Content file was not a PDF")
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            raise ValueError("Content PDF is encrypted")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("Content PDF exceeds the local page limit")
        parts = []
        for page in reader.pages:
            text = (
                (
                    page.extract_text(extraction_mode="layout", layout_mode_space_vertically=False)
                    or ""
                ).strip()
                if "/Contents" in page
                else ""
            )
            if not text:
                warnings.append("A PDF page has no readable text; check the original file.")
            parts.append(text)
        text = "\n\n".join(parts).strip()
    elif mime in {"text/html", "text/plain"}:
        # A decoding failure is a read failure, not an empty description.
        text = body.decode("utf-8-sig")
        if mime == "text/html":
            text = html_text(text)
        text = text.strip()
    else:
        raise ValueError("Content file type is not supported for text reading")
    if not text:
        raise ValueError("Content file has no readable text")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError("Content text exceeds the local reading limit")
    return text, warnings


async def enrich_content(get, download, base_url, le_version, course, task, result):
    kind = "modules" if "ModuleCO-" in (task.url or "") else "topics"
    item_id = task.external_id.removeprefix("content-")
    unavailable = task.raw_data.setdefault("unavailable_fields", [])
    if "description" not in unavailable:
        unavailable.append("description")
    try:
        raw = await get(
            f"/d2l/api/le/{le_version}/{course.external_id}/content/{kind}/{item_id}",
            None,
        )
        if (
            not isinstance(raw, dict)
            or str(raw.get("Id")) != item_id
            or raw.get("Type") != (0 if kind == "modules" else 1)
            or raw.get("Title") != task.title
            or raw.get("IsHidden") is not False
            or raw.get("IsLocked") is not False
        ):
            raise ValueError("Content metadata identity or visibility could not be confirmed")
        description = rich_text(raw.get("Description"))
        file_path = static_file_path(raw.get("Url", ""), base_url, course.external_id)
        evidence, warnings = "content_metadata", []
        if file_path:
            if download is None:
                raise ValueError("Content file text is unavailable for this connection")
            body, content_type = await download(file_path)
            file_text, warnings = await asyncio.to_thread(document_text, body, content_type)
            description = "\n\n".join(part for part in (description, file_text) if part)
            evidence = "content_file_text"
        elif raw.get("Url"):
            task.raw_data["linked_content_unread"] = True
            if not description:
                raise ValueError("Linked content has no readable description")
            result.warnings.append(
                f"{course.name}: {task.title} — linked content beyond its description was not read."
            )
        task.description = description
        task.raw_data["description_evidence"] = evidence
        if warnings:
            result.warnings.extend(
                f"{course.name}: {task.title} — {warning}" for warning in set(warnings)
            )
        else:
            unavailable.remove("description")
    except TransportError as exc:
        body_warning(result, course, task.title, f"Content bodies: {exc.safe_message}")
    except Exception:
        # PDF parsers can raise several malformed-file exceptions. Keep prior evidence;
        # never put raw parser errors, file URLs or document fragments in diagnostics.
        body_warning(result, course, task.title, "Content bodies could not be fully read")
