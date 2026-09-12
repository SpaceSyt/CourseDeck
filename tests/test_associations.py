import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coursedeck.associations import AssociationStore, build_router, source_identity
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult, Task, now
from coursedeck.knowledge import KnowledgeStore
from coursedeck.mail import MailMessage, MailStore


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "tasks.db")
    knowledge = KnowledgeStore(tmp_path / "knowledge.db")
    mail = MailStore(db)
    courses = [
        Course(provider=p, external_id="math", name="Calculus") for p in ["gradescope", "webassign"]
    ]
    tasks = [
        Task(
            provider="gradescope",
            course_external_id="math",
            external_id="123",
            title="Homework 1",
            url="https://www.gradescope.com/courses/math/assignments/123",
            submission_status="submitted",
        ),
        Task(
            provider="webassign",
            course_external_id="math",
            external_id="456",
            title="HW 1",
            url="https://www.webassign.net/web/Student/Assignment-Responses/last?dep=456",
            submission_status="open",
        ),
    ]
    for course, task in zip(courses, tasks, strict=True):
        db.apply(
            course.provider,
            SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=[task]),
            now().isoformat(),
        )
    db.merge_courses(courses[0].id, [courses[1].id])
    return db, knowledge, mail, courses, tasks, AssociationStore(db, knowledge, mail)


def apply(db, course, tasks):
    db.apply(
        course.provider,
        SyncResult(outcome=Outcome.SUCCESS, courses=[course], tasks=tasks),
        now().isoformat(),
    )


def test_stable_views_do_not_reload_inventory_or_write(setup, monkeypatch):
    db, _, _, _, tasks, store = setup
    calls = 0
    inventory = store._inventory

    def counted():
        nonlocal calls
        calls += 1
        return inventory()

    monkeypatch.setattr(store, "_inventory", counted)
    store.view(tasks[0].id)
    expected = store.view(tasks[0].id)
    baseline = calls
    before = store._task_revision.token()
    for _ in range(25):
        assert store.view(tasks[0].id) == expected
    assert calls == baseline
    assert store._task_revision.token() == before
    # Local data mutations are observed even when there is no explicit hook.
    db.patch_local(tasks[0].id, {"note": "saved locally"})
    store.view(tasks[0].id)
    assert calls == baseline + 1


def test_cache_invalidates_for_all_association_inputs(setup, monkeypatch):
    db, knowledge, mail, courses, tasks, store = setup
    calls = 0
    inventory = store._inventory

    def counted():
        nonlocal calls
        calls += 1
        return inventory()

    monkeypatch.setattr(store, "_inventory", counted)

    def change(mutation):
        store.view(tasks[0].id, True)
        store.view(tasks[0].id, True)
        previous = calls
        mutation()
        result = store.view(tasks[0].id, True)
        assert calls > previous
        return result

    tasks[1].submission_status = "submitted"
    result = change(lambda: apply(db, courses[1], [tasks[1]]))
    assert result["relations"][0]["target"]["status"] == "submitted"
    change(lambda: mail.upsert(MailMessage(id="m", sender="Teacher", subject="Hello")))
    result = change(lambda: mail.patch("m", {"deleted": True}))
    assert next(item for item in result["available"] if item["id"] == "m")["status"] == "Deleted"
    change(lambda: mail.reset_defaults())
    change(
        lambda: knowledge.upsert_documents(
            [
                {
                    "id": "doc",
                    "provider": "gradescope",
                    "course_id": courses[0].id,
                    "title": "Homework 1",
                    "body": "Details",
                }
            ]
        )
    )
    group = next(c["workspace_id"] for c in db.courses() if c["id"] == courses[0].id)
    result = change(lambda: db.patch_course(group, {"disabled": True}))
    assert not any(item["active"] for item in result["relations"])
    change(lambda: db.patch_course(group, {"disabled": False}))
    target = db.add_course("New group", "gradescope", None)
    change(lambda: db.merge_courses(target, [group]))
    other = AssociationStore(db, knowledge, mail)
    try:
        result = change(lambda: other.decide(tasks[0].id, "task", tasks[1].id, "reject"))
        assert (
            next(r for r in result["relations"] if r["target"]["id"] == tasks[1].id)["state"]
            == "rejected"
        )
    finally:
        other.close()


def test_index_tasks_writes_only_changed_documents(setup, monkeypatch):
    db, knowledge, _, _, tasks, _ = setup
    assert knowledge.index_tasks(db) == 2
    assert knowledge.index_tasks(db) == 0
    original = db.tasks
    reads = 0

    def counted():
        nonlocal reads
        reads += 1
        return original()

    monkeypatch.setattr(db, "tasks", counted)
    for _ in range(25):
        assert knowledge.index_tasks(db) == 0
    assert reads == 0
    db.patch_local(tasks[0].id, {"note": "Not exposed implicitly to the model"})
    assert knowledge.index_tasks(db) == 0
    assert reads == 1
    db.patch_local(tasks[0].id, {"completion_override": "done"})
    assert knowledge.index_tasks(db) == 1
    assert knowledge.index_tasks(db) == 0
    with knowledge.connection() as connection:
        connection.execute("DELETE FROM documents WHERE id=?", ("task:" + tasks[0].id,))
    assert knowledge.index_tasks(db) == 1
    assert knowledge.get("task:" + tasks[0].id) is not None


def test_task_documents_preserve_explicit_field_uncertainty(setup):
    db, knowledge, _, courses, tasks, _ = setup
    tasks[0].raw_data = {"field_availability": {"due_at": "unknown"}}
    apply(db, courses[0], [tasks[0]])
    knowledge.index_tasks(db)
    document = knowledge.get("task:" + tasks[0].id)
    assert document["complete"] is False
    assert document["source_fields"]["field_availability"]["due_at"] == "unknown"
    assert document["source_fields"]["source_state"] == "done"
    assert any("due at" in warning for warning in document["warnings"])


def test_number_is_candidate_not_merge_and_decision_is_durable(setup):
    db, _, _, _, tasks, store = setup
    before = db.tasks()
    relation = store.view(tasks[0].id)["relations"][0]
    assert relation["state"] == "suggested" and relation["target"]["status"] == "open"
    store.decide(tasks[0].id, "task", tasks[1].id, "confirm")
    assert store.view(tasks[1].id)["relations"][0]["state"] == "confirmed"
    assert db.tasks() == before
    store.decide(tasks[1].id, "task", tasks[0].id, "unlink")
    assert store.view(tasks[0].id)["relations"] == []
    retained = AssociationStore(db, store.knowledge).view(tasks[0].id, True)["relations"][0]
    assert retained["state"] == "rejected" and not retained["active"]
    store.decide(tasks[0].id, "task", tasks[1].id, "link")
    assert store.view(tasks[0].id)["relations"][0]["active"]


def test_exact_url_links_mail_material_and_tasks_without_mutating_source(setup):
    db, knowledge, mail, courses, tasks, store = setup
    tasks[1].description = '<a href="' + tasks[0].url + '?token=SECRET">Submit</a>'
    apply(db, courses[1], [tasks[1]])
    mail.upsert(
        MailMessage(
            id="message",
            sender="Teacher",
            subject="Calculus reminder",
            body=tasks[0].url,
            body_complete=True,
        )
    )
    knowledge.upsert_documents(
        [
            {
                "id": "material",
                "provider": "webassign",
                "course_id": courses[1].id,
                "title": "Instructions",
                "body": tasks[0].url,
                "url": "javascript:alert(1)",
                "complete": False,
            }
        ]
    )
    result = store.view(tasks[0].id)
    assert len(result["relations"]) == 3
    assert all(relation["state"] == "automatic" for relation in result["relations"])
    doc = next(r["target"] for r in result["relations"] if r["target"]["kind"] == "document")
    assert doc["url"] is None and doc["status"] == "Incomplete"
    before = result["relations"]
    assert store.view(tasks[0].id)["relations"] == before
    assert "SECRET" not in str(result)


def test_no_fuzzy_titles_cross_course_links_or_course_home_links(setup):
    db, knowledge, _, courses, tasks, store = setup
    tasks[1].title = "Very similar Homework"  # No explicit number.
    tasks[0].title = "Very similar homework"
    for course, task in zip(courses, tasks, strict=True):
        apply(db, course, [task])
    knowledge.upsert_documents(
        [
            {
                "id": "other",
                "provider": "gradescope",
                "course_id": "unknown",
                "title": "Homework",
                "body": tasks[0].url,
            }
        ]
    )
    assert store.view(tasks[0].id)["relations"] == []
    assert source_identity("https://www.gradescope.com/courses/math") is None
    assert source_identity("https://www.webassign.net/web/Student/Assignments?dep=456") is None
    assert source_identity("https://evil.example/courses/math/assignments/123") is None


def test_duplicate_source_ids_and_attempts_remain_separate_candidates(setup):
    db, _, _, courses, tasks, store = setup
    duplicate = tasks[0].model_copy(
        update={"external_id": "duplicate", "title": "Homework 1 Attempt 2"}
    )
    apply(db, courses[0], [tasks[0], duplicate])
    tasks[1].description = tasks[0].url
    apply(db, courses[1], [tasks[1]])
    result = store.view(tasks[1].id)["relations"]
    assert len(result) == 2 and all(r["state"] == "suggested" for r in result)
    assert {r["target"]["id"] for r in result} == {tasks[0].id, duplicate.id}


def test_scope_disabled_restored_and_chained_merge_keep_history(setup):
    db, knowledge, _, courses, tasks, store = setup
    group = next(c["workspace_id"] for c in db.courses() if c["id"] == courses[0].id)
    knowledge.upsert_documents(
        [
            {
                "id": "doc",
                "provider": "gradescope",
                "course_id": group,
                "title": "Worksheet",
                "source_task_id": tasks[0].id,
            }
        ]
    )
    store.decide(tasks[0].id, "document", "doc", "link")
    db.patch_course(group, {"disabled": True})
    assert not any(r["active"] for r in store.view(tasks[0].id)["relations"])
    db.patch_course(group, {"disabled": False})
    target = db.add_course("Unified", "gradescope", None)
    db.merge_courses(target, [group])
    relation = next(r for r in store.view(tasks[0].id)["relations"] if r["target"]["id"] == "doc")
    assert relation["active"] and relation["state"] == "confirmed"
    assert "inactive" in {event["action"] for event in relation["history"]}
    db.patch_course(target, {"deleted": True})
    assert not any(r["active"] for r in store.view(tasks[0].id)["relations"])


def test_detached_course_deactivates_manual_links_and_rejects_cross_scope(setup):
    db, _, _, courses, tasks, store = setup
    store.decide(tasks[0].id, "task", tasks[1].id, "confirm")
    group = db.courses()[0]["workspace_id"]
    db.update_course_sources(group, "Math", [courses[0].id])
    assert not store.view(tasks[0].id)["relations"][0]["active"]
    with pytest.raises(ValueError):
        store.decide(tasks[0].id, "task", tasks[1].id, "link")


def test_api_validates_inputs_and_returns_independent_status(setup):
    _, _, _, _, tasks, store = setup
    app = FastAPI()
    app.include_router(build_router(store))
    client = TestClient(app)
    path = "/api/associations/task/" + tasks[0].id
    assert client.get(path).status_code == 200
    assert client.get("/api/associations/task/missing").status_code == 404
    assert (
        client.post(
            path, json={"target_kind": "task", "target_id": tasks[1].id, "action": "delete"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, json={"target_kind": "task", "target_id": tasks[0].id, "action": "link"}
        ).status_code
        == 400
    )
    response = client.post(
        path, json={"target_kind": "task", "target_id": tasks[1].id, "action": "confirm"}
    )
    assert (
        response.status_code == 200
        and response.json()["relations"][0]["target"]["status"] == "open"
    )


def test_missing_document_keeps_history_and_can_be_unlinked(setup):
    _, knowledge, _, courses, tasks, store = setup
    knowledge.upsert_documents(
        [
            {
                "id": "doc",
                "provider": "gradescope",
                "course_id": courses[0].id,
                "title": "Instructions",
                "source_task_id": tasks[0].id,
            }
        ]
    )
    store.decide(tasks[0].id, "document", "doc", "link")
    with knowledge.connection() as conn:
        conn.execute("DELETE FROM documents WHERE id='doc'")
    relation = next(r for r in store.view(tasks[0].id)["relations"] if r["target"]["id"] == "doc")
    assert not relation["active"] and relation["target"]["status"] == "Unavailable"
    store.decide(tasks[0].id, "document", "doc", "unlink")
    relation = next(
        r for r in store.view(tasks[0].id, True)["relations"] if r["target"]["id"] == "doc"
    )
    assert relation["state"] == "rejected"


def test_reject_survives_number_becoming_explicit_url(setup):
    db, _, _, courses, tasks, store = setup
    store.decide(tasks[0].id, "task", tasks[1].id, "reject")
    tasks[1].description = tasks[0].url
    apply(db, courses[1], [tasks[1]])
    result = store.view(tasks[0].id, True)["relations"][0]
    assert result["state"] == "rejected" and not result["active"]
    assert result["evidence"][0]["kind"] == "source_url"


def test_number_in_material_body_suggested_and_not_just_title(setup):
    _, knowledge, _, courses, tasks, store = setup
    knowledge.upsert_documents(
        [
            {
                "id": "doc",
                "provider": "gradescope",
                "course_id": courses[0].id,
                "title": "Weekly instructions",
                "body": "For homework #1 read the rubric.",
            }
        ]
    )
    relation = next(r for r in store.view(tasks[0].id)["relations"] if r["target"]["id"] == "doc")
    assert relation["state"] == "suggested"


def test_explicit_platform_stable_id_links_without_task_url(setup):
    db, _, _, courses, tasks, store = setup
    tasks[0].url = None
    tasks[1].description = "Upload to Gradescope assignment ID: 123."
    apply(db, courses[0], [tasks[0]])
    apply(db, courses[1], [tasks[1]])
    relation = store.view(tasks[0].id)["relations"][0]
    assert relation["state"] == "automatic"
    assert relation["evidence"][0]["kind"] == "source_id"


def test_document_context_never_reads_mail_or_rebuilds_and_honors_rejection(setup, monkeypatch):
    db, knowledge, mail, courses, tasks, store = setup
    knowledge.upsert_documents(
        [
            {
                "id": "doc",
                "provider": "gradescope",
                "course_id": courses[0].id,
                "title": "Instructions",
                "source_task_id": tasks[0].id,
            }
        ]
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Chat document context must not read email or rebuild")

    with monkeypatch.context() as patch:
        patch.setattr(mail, "rules", forbidden)
        patch.setattr(store, "rebuild", forbidden)
        assert store.linked_document_ids(tasks[0].id) == {"doc"}
    store.decide(tasks[0].id, "document", "doc", "reject")
    assert store.linked_document_ids(tasks[0].id) == set()
    store.decide(tasks[0].id, "document", "doc", "link")
    db.patch_course(courses[0].id, {"disabled": True})
    assert store.linked_document_ids(tasks[0].id) == set()


def test_filename_number_is_only_a_candidate(setup):
    db, _, _, courses, tasks, store = setup
    tasks[0].title = "CS0000_HW2_README"
    tasks[1].title = "Homework 2"
    for course, task in zip(courses, tasks, strict=True):
        apply(db, course, [task])
    result = store.view(tasks[0].id)["relations"][0]
    assert result["state"] == "suggested"
    assert result["evidence"][0]["kind"] == "assignment_number"


def test_unclassified_mail_unique_url_links_without_classification_change(setup):
    db, _, mail, courses, tasks, store = setup
    mail.upsert(
        MailMessage(
            id="unclassified",
            sender="Teacher",
            subject="Reminder",
            body=tasks[0].url,
            body_complete=False,
        )
    )
    before = mail.get("unclassified")
    assert before["course_id"] is None
    relation = next(
        r for r in store.view(tasks[0].id)["relations"] if r["target"]["id"] == "unclassified"
    )
    assert relation["state"] == "automatic" and relation["active"]
    assert mail.get("unclassified") == before
    db.patch_course(courses[0].id, {"disabled": True})
    assert not next(
        r for r in store.view(tasks[0].id)["relations"] if r["target"]["id"] == "unclassified"
    )["active"]


def test_unclassified_mail_multiple_explicit_assignments_do_not_infer_course(setup):
    _, _, mail, _, tasks, store = setup
    mail.upsert(
        MailMessage(
            id="ambiguous",
            sender="Teacher",
            subject="Reminders",
            body=tasks[0].url + "\n" + tasks[1].url,
            body_complete=False,
        )
    )
    assert not any(r["target"]["id"] == "ambiguous" for r in store.view(tasks[0].id)["relations"])


def test_brightspace_content_home_preserves_type_and_item_identity():
    prefix = "https://school.edu/d2l/le/content/1/Home?itemIdentifier=D2L.LE.Content.ContentObject."
    topic = source_identity(prefix + "TopicCO-123")
    assert topic == source_identity("https://school.edu/d2l/le/content/1/viewContent/123/View")
    assert topic != source_identity(prefix + "TopicCO-124")
    assert topic != source_identity(prefix + "ModuleCO-123")
    assert source_identity("https://school.edu/d2l/le/content/1/Home") is None
    assert source_identity(prefix + "TopicCO-123&itemIdentifier=other") is None


def test_repeated_decisions_do_not_append_history(setup):
    _, _, _, _, tasks, store = setup
    for action in ["confirm", "link", "reject", "unlink"]:
        first = store.decide(tasks[0].id, "task", tasks[1].id, action)
        assert store.decide(tasks[0].id, "task", tasks[1].id, action) == first


@pytest.mark.parametrize(
    "first,second",
    [
        (
            "https://classroom.google.com/u/0/c/abc/a/def/details",
            "https://classroom.google.com/c/abc/a/def/details",
        ),
        (
            "https://school.edu/d2l/lms/dropbox/user/folder_submit_files.d2l?ou=1&db=2",
            "https://school.edu/d2l/lms/dropbox/user/folder_submit_files.d2l?db=2&ou=1&token=secret",
        ),
    ],
)
def test_stable_source_routes_ignore_only_nonidentity_parameters(first, second):
    assert source_identity(first) is not None and source_identity(first) == source_identity(second)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:pass@www.gradescope.com/courses/1/assignments/2",
        "https://www.webassign.net/web/Student/Assignment-Responses/last?dep=1&dep=2",
    ],
)
def test_unsafe_or_ambiguous_urls_are_not_evidence(url):
    assert source_identity(url) is None
