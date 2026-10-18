import json
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from coursedeck.cache_query import CacheDetail, CachePage, CacheQuery, readonly_connection
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task
from coursedeck.knowledge import KnowledgeStore
from coursedeck.mail import MailMessage, MailStore


@pytest.fixture
def cache(tmp_path):
    db = Database(tmp_path / "coursedeck.sqlite3")
    knowledge = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    course = Course(provider="gradescope", external_id="1", name="Algebra")
    stamp = datetime.now(UTC).isoformat()
    tasks = [
        Task(
            provider="gradescope",
            external_id=str(index),
            course_external_id="1",
            title=f"Worksheet {index}",
            description="long text " * 1000,
            submission_status="graded" if index == 1 else "open",
            score=0 if index == 1 else None,
            points_possible=10,
            graded=index == 1,
            due_at=datetime(2026, 10, 4, tzinfo=UTC) if index == 1 else None,
            url="https://www.gradescope.com/courses/1/assignments/1?token=secret&view=student",
            raw_data={"private_token": "redact me", "local_path": "C:/secret.txt"},
        )
        for index in (1, 2)
    ]
    db.apply(
        "gradescope",
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=tasks, complete=True),
        stamp,
    )
    db.update_state("gradescope", last_outcome="success", last_successful_sync=stamp)
    knowledge.upsert_documents(
        [
            {
                "id": "doc-1",
                "provider": "gradescope",
                "course_id": course.id,
                "title": "Lecture Algebra",
                "body": "cached document " * 50,
                "fetched_at": stamp,
                "checked_at": stamp,
                "complete": False,
                "warnings": ["Some pages unavailable"],
                "url": "https://example.edu/document",
            }
        ]
    )
    MailStore(db).upsert(
        MailMessage(
            id="mail-1",
            sender="Staff",
            sender_email="staff@example.edu",
            subject="Worksheet released",
            snippet="Algebra worksheet",
            body="Private cached body " * 50,
            body_complete=True,
            body_checked_at=datetime.now(UTC),
            unread=True,
            url="https://mail.google.com/mail/u/0/#inbox/mail-1",
        )
    )
    query = CacheQuery(db.path, knowledge.path)
    yield query, db, knowledge, tasks
    knowledge.close()


def fingerprint(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def test_all_reads_preserve_files_schema_rules_and_local_states(cache, monkeypatch):
    query, db, knowledge, tasks = cache
    before = fingerprint(db.path.parent)

    def forbidden(*args, **kwargs):
        raise AssertionError("A read tried to initialize a store")

    monkeypatch.setattr(Database, "__init__", forbidden)
    monkeypatch.setattr(KnowledgeStore, "__init__", forbidden)
    monkeypatch.setattr(KnowledgeStore, "index_tasks", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    # Constructors do not initialize or migrate even when invoked after the guard.
    query = CacheQuery(db.path, knowledge.path)
    pages = [
        query.search_tasks(),
        query.search_courses(),
        query.search_grades(),
        query.search_documents(),
        query.search_mail(),
        query.list_changes(),
        query.health(),
    ]
    assert all(isinstance(page, CachePage) for page in pages)
    details = [query.get_task(tasks[0].id), query.read_mail("mail-1"), query.read_document("doc-1")]
    assert all(isinstance(detail, CacheDetail) for detail in details)
    assert fingerprint(db.path.parent) == before


def test_task_pagination_filters_completion_and_zero_grade(cache):
    query, _, _, tasks = cache
    first = query.search_tasks(limit=1)
    second = query.search_tasks(offset=first.pagination.next_offset, limit=1)
    assert first.pagination.total == 2
    assert first.items[0].id != second.items[0].id
    assert not second.pagination.has_more
    graded = query.search_grades(query="Worksheet 1").items[0]
    assert graded.data["score"] == 0
    assert graded.data["is_completed"] is True
    assert graded.meta.field_availability["score"] == "known"
    assert query.search_tasks(completed=True).items[0].id == tasks[0].id
    dates = query.search_tasks(due_before="2026-10-05T00:00:00Z")
    assert len(dates.items) == 1
    assert "due date unavailable" in dates.warnings[0]
    assert query.search_tasks(query="' OR 1=1 --").pagination.total == 0


def test_complete_task_json_reassembles_and_redacts(cache):
    query, _, _, tasks = cache
    offset, parts = 0, []
    while True:
        detail = query.get_task(tasks[0].id, offset=offset, limit=1700)
        parts.append(detail.content)
        if not detail.pagination.has_more:
            break
        assert detail.item.meta.truncated
        offset = detail.pagination.next_offset
    task = json.loads("".join(parts))
    assert task["description"] == tasks[0].description
    assert task["score"] == 0 and task["is_completed"]
    assert task["raw_data"] == {}
    assert "token=" not in task["url"]


def test_document_body_partial_and_freshness_distinct_from_page(cache):
    query, _, _, _ = cache
    result = query.search_documents(query="Algebra", completeness="incomplete")
    assert result.items[0].meta.freshness == "recent"
    assert result.items[0].meta.partial
    assert result.items[0].meta.last_confirmed_at is None
    result = query.read_document("doc-1", offset=3, limit=9)
    assert result.content == ("cached document " * 50)[3:12]
    assert result.pagination.next_offset == 12
    assert result.item.meta.truncated


def test_mail_query_scope_body_paging_and_unknown_received_date(cache):
    query, _, _, _ = cache
    assert query.search_mail(sender="staff@example.edu").pagination.total == 1
    assert query.search_mail(sender="staff@example").pagination.total == 0
    filtered = query.search_mail(before="2026-12-01")
    assert filtered.pagination.total == 0 and filtered.warnings
    read = query.read_mail("mail-1", limit=7)
    assert read.content == "Private"
    assert read.item.data["unread"] is True
    # Mail schema does not store independent collected_at; do not invent it.
    assert read.item.meta.last_collected_at is None


def test_health_does_not_claim_success_means_complete(cache):
    query, _, _, _ = cache
    health = query.health().items[0]
    assert health.data["last_outcome"] == "success"
    assert health.data["coverage_verified"] is False
    assert health.meta.completeness == "unknown"


@pytest.mark.parametrize("provider", ["google_classroom", "webassign", "rephactor"])
def test_health_includes_failed_sources_without_cached_tasks(tmp_path, provider):
    db = Database(tmp_path / "empty.sqlite3")
    db.update_state(provider, last_outcome="auth_required", warnings=["Login required"])
    result = CacheQuery(db.path).health(provider=provider)
    assert result.pagination.total == 1
    assert result.items[0].provider == provider
    assert result.items[0].data["cached_task_count"] == 0
    assert result.items[0].data["last_outcome"] == "auth_required"
    assert result.items[0].meta.partial


def test_failed_sync_finish_does_not_refresh_health_freshness(tmp_path):
    db = Database(tmp_path / "health.sqlite3")
    stamp = datetime.now(UTC).isoformat()
    db.update_state(
        "webassign",
        last_outcome="auth_required",
        last_finished_sync=stamp,
        last_attempted_sync=stamp,
        last_successful_sync="2020-01-01T00:00:00+00:00",
    )
    record = CacheQuery(db.path).health(provider="webassign").items[0]
    assert record.meta.freshness == "stale"
    assert record.meta.last_collected_at == "2020-01-01T00:00:00+00:00"
    assert record.meta.last_attempted_at == stamp
    assert record.data["last_finished_sync"] == stamp


@pytest.mark.parametrize("reliable_time", [False, True])
def test_legacy_task_documents_do_not_reuse_list_freshness(cache, reliable_time):
    query, db, knowledge, _ = cache
    stamp = datetime.now(UTC).isoformat()
    fields = {"description_fetched_at": stamp} if reliable_time else {}
    knowledge.upsert_documents(
        [
            {
                "id": "task:gradescope:1:legacy",
                "provider": "gradescope",
                "course_id": "gradescope:1",
                "title": "Legacy evidence",
                "kind": "assignment",
                "body": "Retained old body",
                "fetched_at": stamp,
                "checked_at": stamp,
                "complete": True,
                "source_fields": fields,
            }
        ]
    )
    before = fingerprint(db.path.parent)
    detail = query.read_document("task:gradescope:1:legacy")
    assert detail.content == "Retained old body"
    assert detail.item.meta.last_attempted_at == stamp
    assert detail.item.meta.last_collected_at == (stamp if reliable_time else None)
    assert detail.item.meta.freshness == ("recent" if reliable_time else "unknown")
    recent = query.search_documents(query="Legacy evidence", freshness="recent")
    unknown = query.search_documents(query="Legacy evidence", freshness="unknown")
    assert recent.pagination.total == int(reliable_time)
    assert unknown.pagination.total == int(not reliable_time)
    if not reliable_time:
        assert any("Legacy task evidence" in w for w in detail.item.meta.warnings)
    assert fingerprint(db.path.parent) == before


def test_absent_cache_and_restore_pending_fail_without_creating_files(tmp_path):
    path = tmp_path / "missing.sqlite3"
    query = CacheQuery(path)
    with pytest.raises(ValueError, match="Cache unavailable"):
        query.search_tasks()
    assert not path.exists()
    assert query.search_documents().warnings
    Database(path)
    journal = tmp_path / "backups" / "restore-pending.json"
    journal.parent.mkdir()
    journal.write_text("{}")
    before = fingerprint(tmp_path)
    with pytest.raises(ValueError, match="recovery"):
        query.search_tasks()
    assert fingerprint(tmp_path) == before


def test_authorizer_blocks_writes_attach_and_pragmas(cache):
    _, db, _, _ = cache
    with readonly_connection(db.path) as connection:
        for statement in (
            "DELETE FROM tasks",
            "PRAGMA user_version=0",
            "ATTACH ':memory:' AS unsafe",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)


def test_wal_snapshot_sees_committed_data_without_touching_original_sidecars(tmp_path):
    path = tmp_path / "wal.sqlite3"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE example(value TEXT)")
        writer.execute("INSERT INTO example VALUES ('committed WAL')")
        writer.commit()
        before = fingerprint(tmp_path)
        with readonly_connection(path) as reader:
            assert reader.execute("SELECT value FROM example").fetchone()[0] == "committed WAL"
        assert fingerprint(tmp_path) == before
    finally:
        writer.close()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"offset": -1},
        {"query": "x" * 2001},
        {"completed": "yes"},
        {"due_before": "2026-10-05"},
    ],
)
def test_invalid_query_arguments_are_rejected(cache, params):
    with pytest.raises(ValueError):
        cache[0].search_tasks(**params)


def test_grade_detail_freshness_does_not_inherit_task_listing_time(cache):
    query, db, _, tasks = cache
    with db.connection() as connection:
        raw = json.loads(
            connection.execute("SELECT payload FROM tasks WHERE id=?", (tasks[0].id,)).fetchone()[0]
        )
        raw["raw_data"]["gradescope_detail"] = {
            "collected_at": "2020-01-01T00:00:00Z",
            "questions": [{"score": 0}],
            "field_availability": {"questions": {"status": "partial"}},
        }
        raw["raw_data"]["gradescope_detail_attempt"] = {
            "attempted_at": "2026-10-02T00:00:00Z",
            "outcome": "auth_required",
            "field_availability": {"questions": {"status": "unavailable"}},
        }
        connection.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(raw), tasks[0].id))
    record = query.search_grades(query="Worksheet 1").items[0]
    assert record.meta.freshness == "recent"
    assert record.data["gradescope_detail"]["freshness"] == "stale"
    assert record.meta.field_availability["gradescope_detail.questions"] == "unavailable"


def test_changing_source_snapshot_fails_after_bounded_retries(tmp_path, monkeypatch):
    source = tmp_path / "changing.sqlite3"
    Database(source)
    original_stat = Path.stat
    ticks = 0

    def changed_stat(path, *args, **kwargs):
        nonlocal ticks
        stat = original_stat(path, *args, **kwargs)
        if path != source:
            return stat
        ticks += 1
        return SimpleNamespace(
            st_dev=stat.st_dev,
            st_ino=stat.st_ino,
            st_size=stat.st_size,
            st_mtime_ns=stat.st_mtime_ns + ticks,
            st_mode=stat.st_mode,
        )

    monkeypatch.setattr(Path, "stat", changed_stat)
    with pytest.raises(ValueError, match="changed while reading"):
        with readonly_connection(source):
            pytest.fail("An unstable snapshot must never be exposed")
    assert ticks <= 10


def test_snapshot_budget_rejects_oversized_source_before_copy(tmp_path):
    source = tmp_path / "oversized.sqlite3"
    with source.open("wb") as handle:
        handle.truncate(256 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="snapshot budget"):
        with readonly_connection(source):
            pytest.fail("Oversized source must not be copied")
