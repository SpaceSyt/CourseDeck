import io
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Lock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from coursedeck import chat_attachments as attachments
from coursedeck.chat_attachments import AttachmentStore, build_attachment_router
from coursedeck.knowledge import KnowledgeStore


def pdf_bytes(*texts, encrypted=False):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 30 700 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(content)
    if encrypted:
        writer.encrypt("private")
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


@pytest.fixture
def store(tmp_path):
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        yield AttachmentStore(knowledge)
    finally:
        knowledge.close()


def bound(store, name="notes.txt", body=b"Local evidence"):
    item = store.upload(name, body)
    cid = store.knowledge.create_conversation("Files", None)
    message = store.knowledge.add_message(cid, "user", "Read this")
    store.bind([item["id"]], cid, message["id"])
    return item, cid, message


def test_pdf_worker_preserves_page_markers_partial_warnings_and_original(store):
    content = pdf_bytes("Alpha evidence", "", "Final evidence")
    item, cid, message = bound(store, "lecture.pdf", content)
    document = store.document(item["id"], cid)
    assert item["pages"] == 3 and not item["complete"]
    assert "[Page 1]\nAlpha evidence" in document["body"]
    assert "[Page 3]\nFinal evidence" in document["body"]
    assert any("Page 2" in warning for warning in item["warnings"])
    assert store.download(item["id"])["content"] == content
    assert store.for_messages(cid)[message["id"]][0]["id"] == item["id"]
    assert "content" not in document and "body" not in store.list(cid)[0]


@pytest.mark.parametrize(
    "content,match",
    [
        (pdf_bytes(""), "no readable text"),
        (pdf_bytes("Encrypted text", encrypted=True), "encrypted"),
        (b"%PDF-1.7\ninvalid", "valid PDF"),
        (b"something else", "valid PDF"),
    ],
)
def test_unreadable_pdf_does_not_persist(store, content, match):
    with pytest.raises(HTTPException, match=match):
        store.upload("file.pdf", content)
    with store.knowledge.connection() as connection:
        assert connection.execute("SELECT count(*) FROM chat_attachments").fetchone()[0] == 0


def test_pdf_worker_timeout_and_pythonw_fallback(store, monkeypatch):
    calls = []

    def timeout(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(attachments.sys, "executable", "C:/Python/pythonw.exe")
    monkeypatch.setattr(attachments.subprocess, "run", timeout)
    with pytest.raises(HTTPException, match="timed out"):
        store.upload("slow.pdf", pdf_bytes("Slow"))
    assert calls[0][0][0].endswith("python.exe")
    assert calls[0][1]["timeout"] == attachments.EXTRACTION_TIMEOUT


def test_ten_pdf_uploads_queue_behind_two_extraction_workers(store, monkeypatch):
    running, release, lock = Event(), Event(), Lock()
    active = 0
    peak = 0

    def extract(args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                running.set()
        try:
            assert release.wait(5)
            return SimpleNamespace(
                returncode=0, stdout=b'{"body":"Evidence","pages":1,"warnings":[]}'
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(attachments, "_EXTRACTORS", BoundedSemaphore(2))
    monkeypatch.setattr(attachments.subprocess, "run", extract)
    with ThreadPoolExecutor(max_workers=10) as executor:
        pending = [
            executor.submit(store.upload, f"file-{index}.pdf", b"%PDF-1.7") for index in range(10)
        ]
        try:
            assert running.wait(5)
        finally:
            release.set()
        uploaded = [item.result(timeout=5) for item in pending]
    assert len({item["id"] for item in uploaded}) == 10
    assert len(store.validate([item["id"] for item in uploaded], None)) == 10
    assert peak == 2


def test_extraction_queue_timeout_preserves_all_permits(store, monkeypatch):
    semaphore = BoundedSemaphore(1)
    semaphore.acquire()
    monkeypatch.setattr(attachments, "_EXTRACTORS", semaphore)
    monkeypatch.setattr(attachments, "EXTRACTION_QUEUE_TIMEOUT", 0)
    with pytest.raises(HTTPException) as error:
        store.upload("busy.pdf", b"%PDF-1.7")
    assert error.value.status_code == 429
    assert not semaphore.acquire(blocking=False)
    semaphore.release()


def test_pdf_page_and_character_limits_report_partial_coverage(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_PAGES", 1)
    limited = attachments._extract_pdf(pdf_bytes("First page", "Second page"))
    assert limited["pages"] == 2 and "Second page" not in limited["body"]
    assert any("first 1 of 2" in warning for warning in limited["warnings"])
    monkeypatch.setattr(attachments, "MAX_CHARACTERS", 15)
    limited = attachments._extract_pdf(pdf_bytes("First page with more words"))
    assert len(limited["body"]) == 15
    assert any("limited to 15" in warning for warning in limited["warnings"])


def test_large_upload_preserves_original_and_reports_text_coverage(store):
    content = b"Readable evidence\n" * 700_000
    assert len(content) > 10 * 1024 * 1024
    item = store.upload("large.txt", content)
    assert item["size"] == len(content)
    assert item["characters"] == 2_000_000
    assert not item["complete"] and "2,000,000" in item["warnings"][0]
    assert store.download(item["id"])["content"] == content


def test_pdf_worker_reads_beyond_200_pages(store):
    item, cid, _ = bound(store, "textbook.pdf", pdf_bytes(*[f"Evidence {i}" for i in range(201)]))
    assert item["pages"] == 201 and item["complete"]
    assert "[Page 201]\nEvidence 200" in store.document(item["id"], cid)["body"]


def test_draft_byte_quota_preserves_existing_uploads(store, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_DRAFT_BYTES", 10)
    first = store.upload("first.txt", b"123456")
    with pytest.raises(HTTPException, match="Too many unsent"):
        store.upload("second.txt", b"78901")
    assert store.download(first["id"])["content"] == b"123456"


def test_conversation_limit_and_invalid_message_leave_drafts_untouched(store, monkeypatch):
    _, cid, message = bound(store)
    draft = store.upload("draft.txt", b"New document")
    monkeypatch.setattr(attachments, "MAX_PER_CONVERSATION", 1)
    with pytest.raises(HTTPException, match="100 files"):
        store.bind([draft["id"]], cid, message["id"])
    assert store.validate([draft["id"]], None)[0]["conversation_id"] is None
    other = store.knowledge.create_conversation("Other", None)
    with pytest.raises(HTTPException, match="user message"):
        store.bind([draft["id"]], other, message["id"])
    assert store.validate([draft["id"]], None)[0]["conversation_id"] is None


@pytest.mark.parametrize(
    "name,body,match",
    [
        ("file.docx", b"data", "Supported files"),
        ("empty.txt", b"", "empty"),
        ("binary.txt", b"\x00hello", "binary"),
        ("latin.txt", b"\xff", "UTF-8"),
        ("blank.md", b"  \r\n", "no readable text"),
    ],
)
def test_invalid_uploads_fail_usefully(store, name, body, match):
    with pytest.raises(HTTPException, match=match):
        store.upload(name, body)


def test_unicode_text_truncation_and_paged_read_are_explicit(store, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_CHARACTERS", 30)
    item, cid, _ = bound(store, "中文.md", ("文档内容" * 10).encode("utf-8"))
    assert item["characters"] == 30 and not item["complete"] and item["warnings"]
    read = store.read(item["id"], cid, 5, 10)
    assert len(read["body"]) == 10 and read["next_offset"] == 15
    assert read["total_characters"] == 30
    with pytest.raises(HTTPException, match="offset"):
        store.read(item["id"], cid, -1)


def test_binding_is_atomic_scoped_reusable_and_cascades(store):
    first, cid, message = bound(store)
    draft = store.upload("second.txt", b"Other evidence")
    other = store.knowledge.create_conversation("Other", None)
    other_message = store.knowledge.add_message(other, "user", "Other message")
    with pytest.raises(HTTPException):
        store.bind([draft["id"], first["id"]], other, other_message["id"])
    assert store.validate([draft["id"]], None)[0]["conversation_id"] is None
    for wrong_cid in (other, None):
        with pytest.raises(HTTPException):
            store.document(first["id"], wrong_cid)
    with pytest.raises(HTTPException):
        store.validate([first["id"]], None)
    with pytest.raises(HTTPException):
        store.delete_draft(first["id"])
    followup = store.knowledge.add_message(cid, "user", "Read again")
    assert store.bind([first["id"]], cid, followup["id"])[0]["message_id"] == message["id"]
    with pytest.raises(RuntimeError), store.knowledge.connection() as connection:
        store.bind([draft["id"]], cid, followup["id"], connection=connection)
        raise RuntimeError("rollback")
    assert store.validate([draft["id"]], None)[0]["conversation_id"] is None
    store.knowledge.delete_conversation(cid)
    with pytest.raises(HTTPException):
        store.download(first["id"])
    store.delete_draft(draft["id"])
    with pytest.raises(HTTPException):
        store.download(draft["id"])


def test_new_chat_retry_recovers_bound_files_without_cross_chat_rebinding(store):
    first, cid, message = bound(store)
    second = store.upload("second.txt", b"Second attachment")
    store.bind([second["id"]], cid, message["id"])
    with pytest.raises(HTTPException) as retry:
        store.validate([first["id"], second["id"]], None)
    assert retry.value.status_code == 409
    assert retry.value.detail == {
        "message": "These files were already sent. Open the saved conversation.",
        "conversation_id": cid,
    }
    other, other_cid, _ = bound(store, "other.txt", b"Other chat")
    draft = store.upload("draft.txt", b"Unsent")
    for ids, target in (
        ([first["id"]], other_cid),
        ([first["id"], other["id"]], None),
        ([first["id"], draft["id"]], None),
        ([first["id"], "missing"], None),
    ):
        with pytest.raises(HTTPException) as blocked:
            store.validate(ids, target)
        assert blocked.value.status_code == 404
        assert isinstance(blocked.value.detail, str)
    assert store.for_messages(cid)[message["id"]][0]["conversation_id"] == cid
    assert store.validate([draft["id"]], None)[0]["conversation_id"] is None


def test_draft_expiry_limits_and_per_turn_limit(store, monkeypatch):
    old = store.upload("old.txt", b"Old")
    with store.knowledge.connection() as connection:
        connection.execute("UPDATE chat_attachments SET created_at='2000' WHERE id=?", (old["id"],))
    store.upload("new.txt", b"New")
    with pytest.raises(HTTPException):
        store.download(old["id"])
    with pytest.raises(HTTPException, match="at most"):
        store.validate([str(index) for index in range(11)], None)
    monkeypatch.setattr(attachments, "MAX_BYTES", 4)
    with pytest.raises(HTTPException) as error:
        store.upload("huge.txt", b"12345")
    assert error.value.status_code == 413


def test_raw_upload_download_and_delete_routes(store, monkeypatch):
    app = FastAPI()
    app.include_router(build_attachment_router(SimpleNamespace(attachments=store)), prefix="/chat")
    with TestClient(app) as client:
        response = client.post(
            "/chat/attachments",
            params={"filename": "../中文\r\n.txt"},
            content="Evidence 证据".encode(),
        )
        assert response.status_code == 200
        item = response.json()
        assert item["name"] == "中文.txt"
        download = client.get(f"/chat/attachments/{item['id']}/download")
        assert download.content == "Evidence 证据".encode()
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert client.delete(f"/chat/attachments/{item['id']}").status_code == 200
        assert client.get(f"/chat/attachments/{item['id']}/download").status_code == 404
        monkeypatch.setattr(attachments, "MAX_BYTES", 4)
        assert (
            client.post("/chat/attachments?filename=large.txt", content=b"12345").status_code == 413
        )
