import json
from types import SimpleNamespace

import pytest

from coursedeck.cache_query import CacheQuery
from coursedeck.chat import ChatMessageInput, ChatService
from coursedeck.chat_tasks import TaskTools
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore, redact_source_text, safe_source_url
from coursedeck.mail import MailMessage, MailStore

MARKER = "SYNTHETIC_NOT_A_REAL_SECRET"


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API-KEY",
        "apiKey",
        "api%5Fkey",
        "access_key",
        "AWSAccessKeyId",
        "X-Amz-Credential",
        "X-Amz-Signature",
        "client_secret",
        "client-key",
        "token",
        "authorization",
        "session_id",
        "key",
        "sig",
        "jwt",
        "assertion",
        "code",
        "ticket",
    ],
)
def test_sensitive_query_names_are_removed_but_navigation_is_preserved(key):
    url = f"https://school.example/file?course=123&{key}={MARKER}&view=student#{MARKER}"
    assert safe_source_url(url) == "https://school.example/file?course=123&view=student"


@pytest.mark.parametrize("userinfo", [f":{MARKER}", MARKER, f"user:{MARKER}", "", ":"])
def test_all_url_userinfo_is_rejected_including_empty_username(userinfo):
    assert safe_source_url(f"https://{userinfo}@school.example/file") is None


@pytest.mark.parametrize("value", [None, 123, {}, [], b"https://example.org", "file:///secret"])
def test_source_url_ignores_non_string_and_non_http_values(value):
    assert safe_source_url(value) is None


async def test_complete_cache_and_chat_task_results_do_not_expose_url_credentials(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    course = Course(provider="brightspace", external_id="123", name="Synthetic course")
    url = f"https://school.example/file?api_key={MARKER}&course=123"
    task = Task(
        provider="brightspace",
        course_external_id="123",
        external_id="hw",
        title="Homework",
        url=url,
        raw_data={
            "reference_url": f"https://:{MARKER}@school.example/file",
            "attachment": {"href": url},
            "links": [url],
            "API_KEY": MARKER,
        },
    )
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    knowledge.index_tasks(db)
    try:
        query = CacheQuery(db.path, knowledge.path)
        result = query.get_task(task.id, limit=32000)
        assert MARKER not in result.model_dump_json()
        complete = json.loads(result.content)
        assert complete["url"] == "https://school.example/file?course=123"
        assert complete["raw_data"]["reference_url"] is None
        assert complete["raw_data"]["links"] == [complete["url"]]
        assert complete["raw_data"]["attachment"]["href"] == complete["url"]
        assert "API_KEY" not in complete["raw_data"]
        service = ChatService(
            db, None, tmp_path, vault=SimpleNamespace(), knowledge_store=knowledge
        )
        task_tools = TaskTools(
            service,
            [course.id],
            ChatMessageInput(message="Read homework", course_id=course.id),
            lambda documents: documents,
        )
        chat_result = await task_tools.run("get_task", {"task_id": task.id})
        assert MARKER not in json.dumps(chat_result)
        # Redaction is a projection; original local evidence is not overwritten.
        assert db.tasks()[0]["url"] == url
    finally:
        knowledge.close()


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "https://school.example/homework#instructions\nRead chapter 7. Submit by Friday.",
            "https://school.example/homework#instructions\nRead chapter 7. Submit by Friday.",
        ),
        (
            f"Assignment link: https://school.example/homework?api_key={MARKER}\nRead chapter 7.",
            "Assignment link: https://school.example/homework\nRead chapter 7.",
        ),
        (
            f"See [homework](https://school.example/hw?api_key={MARKER}&view=student#instructions).\n"
            "Submit by Friday!",
            "See [homework](https://school.example/hw?view=student#instructions).\n"
            "Submit by Friday!",
        ),
        (
            f"Read https://school.example/hw?api_key={MARKER}, then submit.",
            "Read https://school.example/hw, then submit.",
        ),
        (
            f"作业：https://school.example/hw?token={MARKER}。请周五提交。",
            "作业：https://school.example/hw。请周五提交。",
        ),
        (
            f"https://school.example/hw#access_token={MARKER}&section=2\nNext line",
            "https://school.example/hw#section=2\nNext line",
        ),
        (
            f"https://school.example/hw#instructions?api_key={MARKER}&page=2",
            "https://school.example/hw#instructions?page=2",
        ),
        (
            f"URL: https://:{MARKER}@school.example/hw.\nKeep this sentence.",
            "URL: [redacted URL].\nKeep this sentence.",
        ),
        (
            "See https://school.example/wiki/Function_(mathematics)#examples.\tKeep whitespace.",
            "See https://school.example/wiki/Function_(mathematics)#examples.\tKeep whitespace.",
        ),
        (
            f'{{"link":"https://school.example/hw?api%5Fkey={MARKER}&page=1"}}',
            '{"link":"https://school.example/hw?page=1"}',
        ),
        (
            f"<https://school.example/hw?page=1;api_key={MARKER}>\r\nNext",
            "<https://school.example/hw?page=1>\r\nNext",
        ),
        (
            "No URL here. Read chapter 7. Submit by Friday.",
            "No URL here. Read chapter 7. Submit by Friday.",
        ),
    ],
)
def test_url_redaction_preserves_prose_anchors_and_punctuation(text, expected):
    assert redact_source_text(text) == expected
    assert redact_source_text(expected) == expected


def test_every_cache_text_projection_redacts_before_paging_without_mutating_evidence(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    body = (
        "https://school.example/homework#instructions\nRead chapter 7.\n"
        f"Assignment link: https://school.example/homework?api_key={MARKER}#instructions.\n"
        "Submit by Friday."
    )
    expected = body.replace(f"?api_key={MARKER}", "")
    label = f"Assignment: https://school.example/hw?api_key={MARKER}#instructions"
    course = Course(provider="brightspace", external_id="123", name=label)
    task = Task(
        provider="brightspace",
        course_external_id="123",
        external_id="hw",
        title=label,
        description=body,
        raw_data={"explanation": body, "links": [body]},
    )
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    db.update_state("brightspace", warnings=[label])
    knowledge.upsert_documents(
        [
            {
                "id": "prose",
                "provider": "brightspace",
                "course_id": course.id,
                "title": label,
                "body": body,
                "complete": False,
                "warnings": [label],
            }
        ]
    )
    MailStore(db).upsert(
        MailMessage(
            id="mail-prose",
            sender="Teacher",
            subject=label,
            snippet=label,
            body=body,
            body_complete=True,
        )
    )
    try:

        def fingerprint():
            return {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

        before = fingerprint()
        query = CacheQuery(db.path, knowledge.path)
        task_result = json.loads(query.get_task(task.id, limit=32000).content)
        assert task_result["description"] == expected
        assert task_result["raw_data"]["explanation"] == expected
        assert task_result["raw_data"]["links"] == [expected]

        for read, identifier, text in [
            (query.read_document, "prose", expected),
            (query.read_mail, "mail-prose", expected),
            (query.get_task, task.id, json.dumps(task_result, ensure_ascii=False, sort_keys=True)),
        ]:
            chunks, offset = [], 0
            while True:
                page = read(identifier, offset=offset, limit=97)
                assert MARKER not in page.model_dump_json()
                assert page.pagination.total == len(text)
                chunks.append(page.content)
                if page.pagination.next_offset is None:
                    break
                offset = page.pagination.next_offset
            assert "".join(chunks) == text

        for page in [
            query.search_tasks(),
            query.search_grades(),
            query.search_courses(),
            query.search_documents(),
            query.search_mail(),
            query.list_changes(),
            query.health(),
        ]:
            assert MARKER not in page.model_dump_json()
        assert fingerprint() == before
        assert db.tasks()[0]["description"] == body
        assert knowledge.get("prose")["body"] == body
    finally:
        knowledge.close()


def test_collection_result_text_uses_same_safe_projection():
    from coursedeck.collection_jobs import CollectionResult

    text = f"See https://school.example/hw?api_key={MARKER}#instructions.\nLogin needed."
    result = CollectionResult(outcome="partial", message=text, warnings=[text])
    assert MARKER not in result.model_dump_json()
    assert result.message == "See https://school.example/hw#instructions.\nLogin needed."


async def test_stdio_text_results_preserve_instructions_and_redact_prefixed_urls(tmp_path):
    import sys
    from pathlib import Path

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    db = Database(tmp_path / "coursedeck.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    text = (
        "https://school.example/homework#instructions\nRead chapter 7.\n"
        f"Assignment link: https://school.example/homework?api_key={MARKER}.\nSubmit by Friday."
    )
    course = Course(provider="brightspace", external_id="123", name="Course")
    task = Task(
        provider="brightspace",
        course_external_id="123",
        external_id="hw",
        title="Homework",
        description=text,
    )
    db.apply(
        "brightspace",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
        now().isoformat(),
    )
    knowledge.index_tasks(db)
    MailStore(db).upsert(MailMessage(id="prose", sender="Teacher", subject="Homework", body=text))
    knowledge.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coursedeck.mcp_server", "--data-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
    )
    async with Client(params, read_timeout_seconds=30) as client:
        for name, arguments in [
            ("get_task", {"task_id": task.id}),
            ("read_document", {"document_id": "task:" + task.id}),
            ("read_mail", {"mail_id": "prose"}),
        ]:
            result = await client.call_tool(name, arguments)
            assert not result.is_error
            assert MARKER not in result.model_dump_json()
            content = result.structured_content["content"]
            if name == "get_task":
                content = json.loads(content)["description"]
            assert "https://school.example/homework#instructions\nRead chapter 7." in content
            assert "Assignment link: https://school.example/homework.\nSubmit by Friday." in content
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
