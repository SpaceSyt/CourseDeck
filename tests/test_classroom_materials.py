import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as BrowserError

from coursedeck.connectors.classroom_materials import (
    ANNOUNCEMENTS_JS,
    MATERIAL_DETAIL_JS,
    collect_classroom_materials,
    material_document,
    read_google_doc_text,
    reference_url,
)
from coursedeck.domain import Course


def encoded(value):
    return base64.b64encode(value.encode()).decode().rstrip("=")


def material_context():
    course = Course(provider="google_classroom", external_id="123", name="Writing")
    url = f"https://classroom.google.com/c/{encoded('123')}/m/{encoded('456')}/details"
    return course, url


def test_material_is_searchable_knowledge_with_attachment_scope_and_stable_identity():
    course, url = material_context()
    doc, partial = material_document(
        course,
        "456",
        {
            "title": "Syllabus",
            "body": "Office hours: Tuesdays.\nRead chapter one.",
            "links": [
                {
                    "url": "https://docs.google.com/document/d/synthetic/edit?usp=sharing",
                    "title": "Course syllabus",
                }
            ],
        },
        url,
    )
    assert doc["id"] == "google_classroom:123:material:456"
    assert doc["course_id"] == "google_classroom:123"
    assert doc["source_task_id"] is None and doc["kind"] == "material"
    assert "Office hours" in doc["body"] and "Read chapter one." in doc["body"]
    assert "contents not collected" in doc["body"]
    assert "?usp" not in doc["body"] and partial
    assert doc["updated_at"] is None
    assert doc["source_modified_at"] is None
    assert doc["complete"] is False and doc["warnings"]
    assert doc["evidence"] == "classroom_material_page"
    assert datetime.fromisoformat(doc["fetched_at"]).tzinfo is not None
    assert doc["fetched_at"] == doc["checked_at"]


def test_material_rejects_other_course_and_unknown_empty_content():
    course, url = material_context()
    with pytest.raises(ValueError):
        material_document(course, "999", {"title": "Other material", "body": "text"}, url)
    with pytest.raises(ValueError):
        material_document(course, "456", {"title": "Unread material", "body": None}, url)
    doc, partial = material_document(course, "456", {"title": "Empty material", "body": ""}, url)
    assert doc["body"] == "" and not partial


def test_sensitive_links_are_not_persisted_in_body_or_references():
    course, url = material_context()
    doc, partial = material_document(
        course,
        "456",
        {
            "title": "Reading",
            "body": "Read https://example.test/file?access_token=private-secret",
            "links": [{"url": "https://example.test/file?session=secret", "title": "Document"}],
        },
        url,
    )
    assert "secret" not in doc["body"] and partial
    assert "Link available on the source page" in doc["body"]
    assert reference_url("javascript:alert(1)") is None
    assert reference_url("https://user:password@example.test/file") is None
    assert (
        reference_url("https://docs.google.com/document/d/example/edit?resourcekey=secret") is None
    )


async def test_material_reads_attachment_siblings_and_excludes_comment_links():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content("""<div class="EE538"><div>
              <div data-stream-item-id="456"><h1>Syllabus</h1></div>
              <div class="nGi02b">Read this before class.</div></div>
              <div><a href="https://drive.google.com/file/d/synthetic/view?authuser=0">
                Syllabus PDF</a></div>
              <div class="PeGHgb"><a href="https://example.test/private-comment">
                Comment attachment</a></div>
            </div>""")
            detail = await page.evaluate(MATERIAL_DETAIL_JS, "456")
            course, url = material_context()
            doc, partial = material_document(course, "456", detail, url)
            assert "Read this before class." in doc["body"]
            assert "Syllabus PDF" in doc["body"] and partial
            assert "authuser" not in doc["body"] and "private-comment" not in doc["body"]
        finally:
            await browser.close()


def test_exported_attachment_text_is_indexed_and_does_not_claim_link_only():
    course, url = material_context()
    reference = "https://docs.google.com/document/d/synthetic/edit"
    doc, partial = material_document(
        course,
        "456",
        {
            "title": "Syllabus",
            "body": None,
            "links": [{"url": reference, "title": "Syllabus file"}],
            "linked_text": {reference: "Office hours on Tuesday.\nFinal essay due December 14."},
        },
        url,
    )
    assert "Office hours on Tuesday" in doc["body"]
    assert "contents not collected" not in doc["body"] and not partial
    assert doc["complete"] is True and doc["warnings"] == []


@pytest.mark.parametrize("valid_download", [True, False, "signed"])
async def test_google_doc_only_reads_source_generated_export_on_same_document(valid_download):
    page = Mock(goto=AsyncMock(), close=AsyncMock())
    page.get_by_role.return_value = Mock(click=AsyncMock(), hover=AsyncMock())
    url = (
        "https://docs.google.com/document/d/synthetic/export?format=txt"
        if valid_download
        else "https://unexpected.test/export?format=txt"
    )
    if valid_download == "signed":
        url = "https://doc-08-5g-docstext.googleusercontent.com/export/file?format=txt&id=synthetic&token=transient"

    @asynccontextmanager
    async def pending_download(**_kwargs):
        value = asyncio.get_running_loop().create_future()
        value.set_result(Mock(url=url))
        yield Mock(value=value)

    page.expect_download = pending_download
    response = Mock(
        status=200,
        headers={"content-type": "text/plain"},
        text=AsyncMock(return_value="Full syllabus text"),
    )
    context = Mock(new_page=AsyncMock(return_value=page))
    context.request.get = AsyncMock(return_value=response)
    text = await read_google_doc_text(context, "https://docs.google.com/document/d/synthetic/edit")
    assert text == ("Full syllabus text" if valid_download else None)
    assert context.request.get.await_count == int(bool(valid_download))
    page.close.assert_awaited_once()


async def test_announcements_exclude_task_cards_and_comments():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content("""<div class="qhnNic"
                data-include-stream-item-materials data-stream-item-id="123">
              <div class="n4xnA"><div class="n8F6Jd"><div class="pco8Kc">
                Writing workshop on Tuesday.</div></div></div>
              <div class="PeGHgb">Private comment</div></div>
              <div class="qhnNic xo7QFd"
                data-include-stream-item-materials data-stream-item-id="456">
              <div class="n4xnA"><div class="n8F6Jd"><div class="pco8Kc">
                New assignment notification</div></div></div></div>""")
            posts = await page.evaluate(ANNOUNCEMENTS_JS)
            assert posts == [{"id": "123", "body": "Writing workshop on Tuesday."}]
        finally:
            await browser.close()


async def test_new_material_detail_failure_keeps_visible_index_and_continues():
    course, _url = material_context()
    course.source_url = f"https://classroom.google.com/c/{encoded('123')}"
    connector = Mock(expand_list=AsyncMock())

    async def read_page(_page, url):
        if f"/m/{encoded('456')}/details" in url:
            raise BrowserError("Unread material")

    connector.read_page = AsyncMock(side_effect=read_page)
    page = Mock()
    page.locator.return_value.evaluate_all = AsyncMock(
        return_value=[
            {"id": "456", "kind": "Material", "title": "New syllabus"},
            {"id": "789", "kind": "Material", "title": "Readable notes"},
        ]
    )
    page.locator.return_value.wait_for = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            [{"id": "555", "body": "Workshop tomorrow"}],
            {"title": "Readable notes", "body": "Full notes", "links": []},
        ]
    )
    result = await collect_classroom_materials(connector, page, course)
    assert result["complete"] is False
    by_id = {doc["id"]: doc for doc in result["documents"]}
    failed = by_id["google_classroom:123:material:456"]
    assert failed["title"] == "New syllabus" and failed["body"] == ""
    assert failed["complete"] is False and failed["warnings"]
    assert failed["fetched_at"] is None and failed["checked_at"]
    assert failed["source_modified_at"] is None and failed["updated_at"] is None
    assert failed["evidence"] == "classroom_material_index"
    succeeded = by_id["google_classroom:123:material:789"]
    assert succeeded["body"] == "Full notes" and succeeded["complete"] is True
    announcement = by_id["google_classroom:123:announcement:555"]
    assert announcement["complete"] is True and announcement["warnings"] == []
    assert announcement["fetched_at"] and announcement["checked_at"]
    assert announcement["updated_at"] is None
    assert announcement["evidence"] == "classroom_stream"


async def test_link_only_material_marks_both_document_and_collection_incomplete():
    course, _url = material_context()
    course.source_url = f"https://classroom.google.com/c/{encoded('123')}"
    connector = Mock(read_page=AsyncMock(), expand_list=AsyncMock())
    page = Mock()
    page.locator.return_value.evaluate_all = AsyncMock(
        return_value=[
            {"id": "456", "kind": "Material", "title": "Student folder"},
        ]
    )
    page.locator.return_value.wait_for = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            [],
            {
                "title": "Student folder",
                "body": "Updated folder instructions",
                "links": [
                    {"url": "https://drive.google.com/drive/folders/synthetic", "title": "Folder"}
                ],
            },
        ]
    )
    result = await collect_classroom_materials(connector, page, course)
    doc = result["documents"][0]
    assert result["complete"] is False and result["warnings"]
    assert doc["complete"] is False and doc["warnings"]
    assert "Updated folder instructions" in doc["body"]
    assert doc["fetched_at"] and doc["checked_at"]
    assert doc["evidence"] == "classroom_material_page"


async def test_progress_callback_retains_success_before_later_cancellation():
    course, _url = material_context()
    course.source_url = f"https://classroom.google.com/c/{encoded('123')}"
    connector = Mock(expand_list=AsyncMock())

    async def read_page(_page, url):
        if f"/m/{encoded('789')}/details" in url:
            raise asyncio.CancelledError()

    connector.read_page = AsyncMock(side_effect=read_page)
    page = Mock()
    page.locator.return_value.evaluate_all = AsyncMock(
        return_value=[
            {"id": "456", "kind": "Material", "title": "First notes"},
            {"id": "789", "kind": "Material", "title": "Pending notes"},
        ]
    )
    page.locator.return_value.wait_for = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            [],
            {
                "title": "First notes",
                "body": "Successfully read before timeout",
                "links": [],
            },
        ]
    )
    retained = {}

    def retain(document):
        retained[document["id"]] = dict(document)

    with pytest.raises(asyncio.CancelledError):
        await collect_classroom_materials(connector, page, course, on_document=retain)
    first = retained["google_classroom:123:material:456"]
    assert first["body"] == "Successfully read before timeout" and first["complete"] is True
    pending = retained["google_classroom:123:material:789"]
    assert pending["body"] == "" and pending["complete"] is False
    assert pending["title"] == "Pending notes" and pending["evidence"] == "classroom_material_index"
