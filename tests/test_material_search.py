from datetime import timedelta

from coursedeck.domain import now
from coursedeck.knowledge import KnowledgeStore, material_freshness, search_terms


def material(key, **values):
    return {
        "id": key,
        "provider": "brightspace",
        "course_id": "brightspace:math",
        "title": key,
        "body": "",
        "kind": "material",
        "complete": True,
    } | values


def test_title_phrase_word_boundaries_and_unicode_ranking(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [
            material("exact", title="Linear algebra"),
            material("body", title="Lecture 2", body="linear algebra " * 50),
            material("separate", title="Linear methods for algebra"),
            material("false", title="Collaboration"),
            material("chinese", title="微积分的课程安排", body="期末考试复习资料"),
        ]
    )
    assert store.search("LINEAR ALGEBRA")[0]["id"] == "exact"
    assert {d["id"] for d in store.search('"linear algebra"')} == {"exact", "body"}
    assert not store.search("lab")
    assert [d["id"] for d in store.search("微积分课程")] == ["chinese"]
    assert [d["id"] for d in store.search("期末考试")] == ["chinese"]


def test_mixed_script_keywords_preserve_compound_words_and_chinese_bigrams(tmp_path):
    terms = search_terms("我今天数学quiz考什么，看syllabus Calc-2 chain_rule")
    assert "quiz" in terms and "syllabus" in terms
    assert "quiz考什么" not in terms
    assert terms["calc-2"] == 1 and terms["chain_rule"] == 1
    assert terms["数学"] == 0.35 and terms["考什么"] == 1
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [
            material("quiz", title="Quiz 02", body="This week's quiz is differentiation."),
            material("syllabus", title="Calculus syllabus", body="Course calendar"),
            material("mixed", title="数学quiz", body="Exact mixed-script phrase"),
            material("spaced", title="数学 quiz", body="Spaced phrase"),
        ]
    )
    assert {item["id"] for item in store.search("我今天数学quiz考什么，看syllabus")} >= {
        "quiz",
        "syllabus",
    }
    assert [item["id"] for item in store.search('"数学quiz"')] == ["mixed"]


def test_filters_scope_and_unknown_are_distinct_from_incomplete(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    stamp = now()
    store.upsert_documents(
        [
            material("recent", fetched_at=stamp.isoformat()),
            material(
                "stale",
                fetched_at=(stamp - timedelta(days=9)).isoformat(),
                checked_at=stamp.isoformat(),
                complete=False,
                kind="announcement",
            ),
            material("unknown", complete=None, fetched_at=None),
            material("foreign", course_id="classroom:other", provider="google_classroom"),
        ]
    )
    assert material_freshness(store.get("stale"), stamp) == "stale"
    assert [d["id"] for d in store.library(freshness="recent")["documents"]] == ["recent"]
    assert [d["id"] for d in store.library(completeness="unknown")["documents"]] == ["unknown"]
    result = store.library(
        course_ids=["brightspace:math"],
        provider="brightspace",
        kind="announcement",
        freshness="stale",
        completeness="incomplete",
    )
    assert [d["id"] for d in result["documents"]] == ["stale"]
    assert store.library(course_ids=[])["total"] == 0


def test_search_full_body_returns_matching_excerpt_and_stable_paging(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [material(f"{i:02}", body="padding " * 3000 + "tailmarker") for i in range(5)]
    )
    first = store.library("tailmarker", limit=2)
    second = store.library("tailmarker", offset=2, limit=2)
    assert first["total"] == 5
    assert [d["id"] for d in first["documents"] + second["documents"]] == ["00", "01", "02", "03"]
    assert all(
        "tailmarker" in d["body"] and d["body_start"] > 0 and d["body_truncated"]
        for d in first["documents"]
    )
    assert all("tailmarker" in d["body"][:220] for d in first["documents"])


def test_search_reuses_loaded_normalized_bodies_and_invalidates_external_updates(
    tmp_path, monkeypatch
):
    import coursedeck.knowledge as module

    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    stamp = now().isoformat()
    documents = [
        material(str(i), body="微积分 Linear algebra " * 100, updated_at=stamp) for i in range(500)
    ]
    assert store.upsert_documents(documents) == 500
    assert store.upsert_documents(documents) == 0
    store.search("algebra")
    cached = store._search_cache
    original = module.normalized_text
    body_normalizations = 0

    def counted(value):
        nonlocal body_normalizations
        if len(value or "") > 100:
            body_normalizations += 1
        return original(value)

    monkeypatch.setattr(module, "normalized_text", counted)
    assert store.search('"Linear algebra"')
    assert store.search("微积分")
    assert store._search_cache is cached
    assert body_normalizations == 0
    other = KnowledgeStore(store.path)
    try:
        other.upsert_documents([documents[0] | {"body": "newuniqueterm"}])
        assert [item["id"] for item in store.search("newuniqueterm")] == ["0"]
        assert store._search_cache is not cached
        assert body_normalizations == 499
    finally:
        other.close()
        store.close()
