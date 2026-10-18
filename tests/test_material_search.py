from datetime import timedelta

import pytest

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


@pytest.mark.parametrize(
    "query",
    [
        "linear algebra",
        '"linear algebra"',
        "lab",
        "chain_rule Calc-2",
        "微积分课程",
        "数学quiz",
        '"数学quiz"',
        "STRASSE",
        "ＦＯＯ",
        "naïve",
        "aaaa",
        "",
        "---",
        "unfindable",
    ],
)
def test_bounded_search_matches_exhaustive_reference_scores(tmp_path, query):
    """Protect ranking/boundaries while replacing exhaustive occurrence lists."""
    import re

    from coursedeck.knowledge import normalized_text

    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    bodies = [
        "linear algebra " * 200 + "tailmarker",
        "linear_algebra collaboration algebraic lab laboratory",
        "数学quiz 数学 quiz 微积分课程安排 " * 50,
        "chain_rule chain_rule2 Calc-2 Calc-22 chain_rule " * 20,
        "Straße STRASSE ＦＯＯ foo naïve naïveté",
        "aaaaa aaa aaaaaaaa",
        "",
    ]
    documents = [
        material(str(index), title=body[:60] or "empty", body=body)
        for index, body in enumerate(bodies)
    ]
    store.upsert_documents(documents)
    terms = search_terms(query)
    phrases = [normalized_text(text).strip() for text in re.findall(r'["“]([^"”]+)["”]', query)]
    phrase = normalized_text(query).strip().strip('"“”')
    expected = []
    for document in documents:
        title, body = normalized_text(document["title"]), normalized_text(document["body"])
        if any(part not in title and part not in body for part in phrases):
            continue
        score, matched = 0.0, []
        for term, weight in terms.items():
            pattern = (
                re.escape(term)
                if re.search(r"[\u3400-\u9fff]", term)
                else r"(?<!\w)" + re.escape(term) + r"(?!\w)"
            )
            title_count, body_count = (
                len(re.findall(pattern, title)),
                len(re.findall(pattern, body)),
            )
            if title_count or body_count:
                matched.append(term)
                score += weight * (20 * min(title_count, 2) + 2 * min(body_count, 5) + 5)
        if phrase and matched:
            score += 100 if title == phrase else 35 if phrase in title else 0
            score += 12 if phrase in body else 0
        if score or not terms:
            expected.append((document["id"], round(score, 2), matched))
    expected.sort(key=lambda item: (-item[1], item[0]))
    try:
        actual = store.search(query, limit=20)
        assert [(d["id"], d["search_score"], d["matched_terms"]) for d in actual] == expected
    finally:
        store.close()


def test_large_scope_streams_and_still_searches_tail_and_pages(tmp_path, monkeypatch):
    import coursedeck.knowledge as module

    monkeypatch.setattr(module, "SEARCH_CACHE_CHAR_LIMIT", 100)
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [material(f"{i:02}", body="padding " * 50 + "tailmarker") for i in range(20)]
    )
    normalizations = []
    original = module.normalized_text

    def counted(value):
        normalizations.append(value)
        return original(value)

    monkeypatch.setattr(module, "normalized_text", counted)
    rows = store._search_rows(None, None, None)
    try:
        assert next(rows)[0]["id"] == "00"
        assert len(normalizations) == 2  # Only one row loaded/normalized so far.
        rows.close()
        assert store._search_cache is None
        page = store.library("tailmarker", offset=18, limit=2)
        assert page["total"] == 20
        assert [d["id"] for d in page["documents"]] == ["18", "19"]
        assert store._search_cache is None
        assert not store.search("unfindable")
    finally:
        rows.close()
        store.close()


def test_commit_during_stream_never_caches_obsolete_snapshot(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents([material(str(i), body="before") for i in range(3)])
    other = KnowledgeStore(store.path)
    rows = store._search_rows(None, None, None)
    try:
        assert next(rows)[0]["body"] == "before"
        other.upsert_documents([material("2", body="after")])
        assert all(row["body"] == "before" for row, _, _ in rows)
        assert store._search_cache is None
        assert [d["id"] for d in store.search("after")] == ["2"]
    finally:
        rows.close()
        other.close()
        store.close()


def test_cached_scope_switches_do_not_leak_other_course_or_provider(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.upsert_documents(
        [
            material("math", body="sharedword"),
            material("other", body="sharedword", course_id="other"),
            material("notice", body="sharedword", kind="announcement"),
            material("classroom", body="sharedword", provider="google_classroom"),
        ]
    )
    try:
        assert len(store.search("sharedword")) == 4
        assert [d["id"] for d in store.search("sharedword", course_ids=["other"])] == ["other"]
        result = store.search(
            "sharedword", ["brightspace:math"], provider="brightspace", kind="material"
        )
        assert [d["id"] for d in result] == ["math"]
        assert len(store.search("sharedword")) == 4
        assert store.search("sharedword", course_ids=[]) == []
    finally:
        store.close()
