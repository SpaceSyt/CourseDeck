import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from coursedeck.connectors.brightspace_content import (
    document_text,
    enrich_content,
    rich_text,
    static_file_path,
)
from coursedeck.connectors.http import TransportError
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now

BASE = "https://school.example"
COURSE = Course(
    provider="brightspace", external_id="1", name="Course", source_url=BASE + "/d2l/home/1"
)


def task():
    return Task(
        provider="brightspace",
        course_external_id="1",
        external_id="content-2",
        title="Instructions",
        url=BASE + "/d2l/le/content/1/Home?itemIdentifier=D2L.LE.Content.ContentObject.TopicCO-2",
    )


def metadata(**changes):
    return {
        "Id": 2,
        "Type": 1,
        "Title": "Instructions",
        "IsHidden": False,
        "IsLocked": False,
        "Description": {"Text": "", "Html": ""},
        "Url": "",
        **changes,
    }


def pdf_bytes(pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 10 200 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = content
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_text_reads_every_page_and_marks_nontext_pages():
    text, warnings = document_text(pdf_bytes(["First page", "Last page"]), "application/pdf")
    assert "First page" in text and "Last page" in text and not warnings
    text, warnings = document_text(pdf_bytes(["Only readable page", None]), "application/pdf")
    assert "Only readable page" in text and warnings
    with pytest.raises(ValueError, match="no readable text"):
        document_text(pdf_bytes([None]), "application/pdf")


def test_description_uses_html_when_plaintext_is_empty():
    assert (
        rich_text({"Text": "", "Html": "<p>Read this.</p><script>Do not keep</script>"})
        == "Read this."
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/content/enforced/1-course/a.pdf",
        "/content/enforced/11-course/a.pdf",
        "/content/enforced/1-course/%2e%2e/private.pdf",
        "/content/enforced/1-course/a.pdf?token=private",
        "/d2l/le/content/1/viewContent/2/View",
    ],
)
def test_static_file_read_rejects_other_courses_signed_urls_and_lms_views(url):
    assert static_file_path(url, BASE, "1") is None


@pytest.mark.asyncio
async def test_content_enrichment_uses_only_static_file_and_preserves_task_dates():
    current = task()
    current.due_at = now()
    original = current.due_at

    async def get(path, params):
        assert path == "/d2l/api/le/1.82/1/content/topics/2"
        return metadata(Url="/content/enforced/1-course/Instructions.pdf")

    async def download(path):
        assert path == "/content/enforced/1-course/Instructions.pdf"
        return pdf_bytes(["Submit on Gradescope. Due Friday."]), "application/pdf"

    result = SyncResult(outcome=Outcome.PARTIAL)
    await enrich_content(get, download, BASE, "1.82", COURSE, current, result)
    assert "Submit on Gradescope" in current.description
    assert current.due_at == original and current.submission_status == "unknown"
    assert "description" not in current.raw_data["unavailable_fields"]
    assert not result.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["404", "identity", "pdf", "hidden"])
async def test_unreadable_content_preserves_cached_description(tmp_path, failure):
    db = Database(tmp_path / "content.sqlite3")
    previous = task()
    previous.description = "Known prior instructions"
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.PARTIAL, courses=[COURSE], tasks=[previous]),
        now().isoformat(),
    )

    async def get(path, params):
        if failure == "404":
            raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 404).")
        return metadata(
            Id=99 if failure == "identity" else 2,
            IsHidden=failure == "hidden",
            Url="/content/enforced/1-course/Instructions.pdf",
        )

    async def download(path):
        return b"not a PDF", "application/pdf"

    current = task()
    result = SyncResult(outcome=Outcome.PARTIAL, courses=[COURSE], tasks=[current])
    await enrich_content(get, download, BASE, "1.82", COURSE, current, result)
    db.apply("brightspace", result, now().isoformat())
    assert db.tasks()[0]["description"] == "Known prior instructions"
    assert result.warnings and "description" in current.raw_data["unavailable_fields"]


@pytest.mark.asyncio
async def test_empty_external_description_preserves_previous_text_without_reading_link():
    current = task()
    current.description = "Previous"

    async def get(path, params):
        return metadata(Url="https://video.example/watch?id=1")

    async def download(path):
        pytest.fail("External links must not be fetched")

    result = SyncResult(outcome=Outcome.PARTIAL)
    await enrich_content(get, download, BASE, "1.82", COURSE, current, result)
    assert current.description == "Previous" and result.warnings
    assert "description" in current.raw_data["unavailable_fields"]


@pytest.mark.asyncio
async def test_unavailable_body_warning_groups_same_course_without_hiding_affected_titles():
    async def get(path, params):
        raise TransportError(Outcome.PARTIAL, "Resource unavailable (HTTP 404).")

    result = SyncResult(outcome=Outcome.PARTIAL)
    for title in ("Homework 2", "Homework 3"):
        current = task()
        current.title = title
        await enrich_content(get, None, BASE, "1.82", COURSE, current, result)
    assert len(result.warnings) == 1
    assert "Homework 2, Homework 3" in result.warnings[0]
    assert "HTTP 404" in result.warnings[0]
