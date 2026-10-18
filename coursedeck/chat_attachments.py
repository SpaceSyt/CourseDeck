"""Conversation-scoped local uploads. Uploading never calls a model or a remote service."""

import io
import json
import re
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response

MAX_BYTES = 50 * 1024 * 1024
MAX_CHARACTERS = 2_000_000
MAX_PAGES = 1000
MAX_PER_TURN = 10
MAX_PER_CONVERSATION = 100
MAX_DRAFT_BYTES = 500 * 1024 * 1024
EXTRACTION_TIMEOUT = 90
EXTRACTION_QUEUE_TIMEOUT = 180
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
}
_EXTRACTORS = BoundedSemaphore(2)
_METADATA_COLUMNS = (
    "id,name,size,media_type,pages,length(body) AS characters,warnings,complete,"
    "created_at,conversation_id,message_id"
)


class _UnreadablePDF(ValueError):
    pass


def _extract_pdf(content):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise _UnreadablePDF("This PDF is encrypted. Upload an unlocked copy.")
        pages = len(reader.pages)
        warnings, parts, count, readable = [], [], 0, False
        if pages > MAX_PAGES:
            warnings.append(f"Only the first {MAX_PAGES} of {pages} pages were read.")
        for index in range(min(pages, MAX_PAGES)):
            try:
                page_text = reader.pages[index].extract_text() or ""
            except Exception:
                warnings.append(f"Page {index + 1} could not be read.")
                page_text = ""
            if not page_text.strip():
                warnings.append(f"Page {index + 1} has no readable text; OCR is not available.")
            else:
                readable = True
            part = f"[Page {index + 1}]\n{page_text.strip()}\n\n"
            remaining = MAX_CHARACTERS - count
            parts.append(part[:remaining])
            count += min(len(part), remaining)
            if len(part) > remaining or (count >= MAX_CHARACTERS and index + 1 < pages):
                warnings.append(
                    f"Extracted text was limited to {MAX_CHARACTERS:,} characters "
                    f"at page {index + 1}."
                )
                break
        if not readable:
            raise _UnreadablePDF(
                "This PDF has no readable text. Upload a PDF with selectable text."
            )
        return {"body": "".join(parts), "pages": pages, "warnings": warnings}
    except _UnreadablePDF:
        raise
    except Exception as error:
        raise ValueError("This PDF could not be read. Check that it is a valid PDF.") from error


def _extract(content, suffix):
    if suffix != ".pdf":
        try:
            body = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise HTTPException(422, "Save this text file as UTF-8 and upload it again.") from error
        if "\x00" in body:
            raise HTTPException(422, "This file contains binary data, not readable text.")
        if not body.strip():
            raise HTTPException(422, "This file contains no readable text.")
        warnings = []
        if len(body) > MAX_CHARACTERS:
            warnings.append(f"Only the first {MAX_CHARACTERS:,} characters were read.")
        return {"body": body[:MAX_CHARACTERS], "pages": None, "warnings": warnings}
    if not content.lstrip().startswith(b"%PDF-"):
        raise HTTPException(422, "This file is not a valid PDF.")
    if not _EXTRACTORS.acquire(timeout=EXTRACTION_QUEUE_TIMEOUT):
        raise HTTPException(429, "Other PDFs are being read. Try again shortly.")
    executable = Path(sys.executable)
    if executable.name.casefold() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    try:
        result = subprocess.run(
            [str(executable), "-m", "coursedeck.chat_attachments", "--extract-pdf"],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=EXTRACTION_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode != 0:
            raise HTTPException(422, "This PDF could not be read. Try a smaller or repaired copy.")
        value = json.loads(result.stdout)
        if value.get("error"):
            raise HTTPException(422, value["error"])
        return value
    except subprocess.TimeoutExpired as error:
        raise HTTPException(422, "Reading this PDF timed out. Try a smaller PDF.") from error
    except (OSError, ValueError) as error:
        raise HTTPException(
            422, "This PDF could not be read. Try a smaller or repaired copy."
        ) from error
    finally:
        _EXTRACTORS.release()


def _metadata(row):
    return dict(row) | {
        "warnings": json.loads(row["warnings"]),
        "complete": bool(row["complete"]),
    }


class AttachmentStore:
    def __init__(self, knowledge):
        self.knowledge = knowledge

    @staticmethod
    def _cleanup(db):
        cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        db.execute(
            "DELETE FROM chat_attachments WHERE conversation_id IS NULL AND created_at < ?",
            (cutoff,),
        )

    def upload(self, filename: str, body: bytes):
        name = re.sub(r"[\x00-\x1f\x7f]", "", filename.replace("\\", "/").split("/")[-1])
        if not name or len(name) > 240:
            raise HTTPException(422, "Use a file name between 1 and 240 characters.")
        suffix = Path(name).suffix.lower()
        if suffix not in MEDIA_TYPES:
            raise HTTPException(415, "Supported files: PDF, TXT, MD, CSV and JSON.")
        if len(body) > MAX_BYTES:
            raise HTTPException(413, "Each file must be 50 MB or smaller.")
        if not body:
            raise HTTPException(422, "This file is empty.")
        extracted = _extract(body, suffix)
        key, stamp = uuid4().hex, datetime.now(UTC).isoformat()
        with self.knowledge.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._cleanup(db)
            count, size = db.execute(
                "SELECT count(*),coalesce(sum(size),0) FROM chat_attachments "
                "WHERE conversation_id IS NULL"
            ).fetchone()
            if count >= 50 or size + len(body) > MAX_DRAFT_BYTES:
                raise HTTPException(409, "Too many unsent files. Send or remove some first.")
            db.execute(
                "INSERT INTO chat_attachments VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    key,
                    name,
                    len(body),
                    MEDIA_TYPES[suffix],
                    extracted["pages"],
                    extracted["body"],
                    json.dumps(extracted["warnings"]),
                    not extracted["warnings"],
                    body,
                    stamp,
                ),
            )
            return _metadata(
                db.execute(
                    f"SELECT {_METADATA_COLUMNS} FROM chat_attachments WHERE id=?", (key,)
                ).fetchone()
            )

    def validate(self, ids, conversation_id, *, connection=None):
        if not isinstance(ids, list) or len(ids) > MAX_PER_TURN:
            raise HTTPException(422, f"Attach at most {MAX_PER_TURN} files per message.")
        if any(not isinstance(key, str) for key in ids) or len(set(ids)) != len(ids):
            raise HTTPException(422, "Attachment IDs must be unique strings.")
        with (
            nullcontext(connection) if connection is not None else self.knowledge.connection() as db
        ):
            selected = []
            for key in ids:
                row = db.execute(
                    f"SELECT {_METADATA_COLUMNS} FROM chat_attachments WHERE id=?", (key,)
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "An attachment is unavailable. Upload it again.")
                selected.append(_metadata(row))
            owners = {item["conversation_id"] for item in selected}
            if conversation_id is None and len(owners) == 1 and None not in owners:
                raise HTTPException(
                    409,
                    {
                        "message": "These files were already sent. Open the saved conversation.",
                        "conversation_id": next(iter(owners)),
                    },
                )
            if any(owner not in (None, conversation_id) for owner in owners):
                raise HTTPException(404, "An attachment is unavailable. Upload it again.")
            if conversation_id:
                count = db.execute(
                    "SELECT count(*) FROM chat_attachments WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
                if (
                    count + sum(item["conversation_id"] is None for item in selected)
                    > MAX_PER_CONVERSATION
                ):
                    raise HTTPException(409, "This chat already has 100 files. Start a new chat.")
            return selected

    def bind(self, ids, conversation_id, message_id, *, connection=None):
        with (
            nullcontext(connection) if connection is not None else self.knowledge.connection() as db
        ):
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            selected = self.validate(ids, conversation_id, connection=db)
            message = db.execute(
                "SELECT conversation_id,role FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if (
                not message
                or message["conversation_id"] != conversation_id
                or message["role"] != "user"
            ):
                raise HTTPException(409, "Attachments must belong to a user message in this chat.")
            for item in selected:
                if item["conversation_id"] is None:
                    db.execute(
                        "UPDATE chat_attachments SET conversation_id=?,message_id=? WHERE id=?",
                        (conversation_id, message_id, item["id"]),
                    )
                    item.update(conversation_id=conversation_id, message_id=message_id)
            return selected

    def list(self, conversation_id):
        with self.knowledge.connection() as db:
            return [
                _metadata(row)
                for row in db.execute(
                    f"SELECT {_METADATA_COLUMNS} FROM chat_attachments "
                    "WHERE conversation_id=? ORDER BY created_at,id",
                    (conversation_id,),
                )
            ]

    def for_messages(self, conversation_id):
        result = {}
        for item in self.list(conversation_id):
            result.setdefault(item["message_id"], []).append(item)
        return result

    def document(self, key, conversation_id):
        if not conversation_id:
            raise HTTPException(404, "Attachment not found in this chat.")
        with self.knowledge.connection() as db:
            row = db.execute(
                f"SELECT {_METADATA_COLUMNS},body FROM chat_attachments "
                "WHERE id=? AND conversation_id=?",
                (key, conversation_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Attachment not found in this chat.")
        metadata = _metadata(row)
        return metadata | {
            "title": metadata["name"],
            "provider": "upload",
            "course_id": None,
            "kind": "uploaded_file",
            "url": None,
            "checked_at": metadata["created_at"],
        }

    def read(self, key, conversation_id, offset=0, limit=12000):
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= 12000
        ):
            raise HTTPException(422, "Use an offset of 0 or more and a limit from 1 to 12000.")
        value = self.document(key, conversation_id)
        body = value.pop("body")
        end = min(len(body), offset + limit)
        return value | {
            "body": body[offset:end],
            "offset": offset,
            "total_characters": len(body),
            "next_offset": end if end < len(body) else None,
        }

    def delete_draft(self, key):
        with self.knowledge.connection() as db:
            row = db.execute(
                "SELECT conversation_id FROM chat_attachments WHERE id=?", (key,)
            ).fetchone()
            if row and row["conversation_id"] is not None:
                raise HTTPException(409, "This file belongs to a sent message.")
            db.execute("DELETE FROM chat_attachments WHERE id=?", (key,))

    def download(self, key):
        with self.knowledge.connection() as db:
            row = db.execute(
                "SELECT name,media_type,content FROM chat_attachments WHERE id=?", (key,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Attachment not found.")
        return dict(row)


def build_attachment_router(service):
    router = APIRouter(prefix="/attachments")

    @router.post("")
    async def upload(request: Request, filename: str = Query(min_length=1, max_length=1024)):
        from .chat import local_io

        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Each file must be 50 MB or smaller.")
            content.extend(chunk)
        return await local_io(service.attachments.upload, filename, bytes(content))

    @router.get("/{attachment_id}/download")
    def download(attachment_id: str):
        value = service.attachments.download(attachment_id)
        return Response(
            value["content"],
            media_type=value["media_type"],
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(value['name'])}",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @router.delete("/{attachment_id}")
    def delete(attachment_id: str):
        service.attachments.delete_draft(attachment_id)
        return {"deleted": True}

    return router


if __name__ == "__main__" and sys.argv[1:] == ["--extract-pdf"]:
    try:
        payload = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("Each file must be 50 MB or smaller.")
        output = _extract_pdf(payload)
    except Exception as error:
        output = {
            "error": str(error) if isinstance(error, ValueError) else "This PDF could not be read."
        }
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=True).encode("utf-8"))
