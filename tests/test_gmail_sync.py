from datetime import UTC, datetime

import pytest

from coursedeck.gmail_sync import (
    advance_history,
    history_page,
    needs_body_refresh,
    parse_received_at,
    select_body_ids,
)


@pytest.mark.parametrize("key", ["", "text-links-v1:", None])
def test_missing_message_identity_never_certifies_unchanged_body(key):
    row = {"content_key": key, "snippet": "same", "date_label": "same", "subject": "same"}
    assert needs_body_refresh(row, row | {"body_complete": True})


def test_body_cache_requires_last_message_identity_and_unchanged_headers():
    row = {
        "content_key": "text-links-v1:123",
        "snippet": "same",
        "date_label": "same",
        "subject": "same",
    }
    cached = row | {"body_complete": True, "body": "retained"}
    assert not needs_body_refresh(row, cached)
    for field in row:
        assert needs_body_refresh(row | {field: "changed"}, cached)
    assert needs_body_refresh(row, cached | {"body_complete": False})
    assert needs_body_refresh(row, None)


@pytest.mark.parametrize(
    "label",
    [
        "Sep 8, 2026, 13:21",
        "Tue, Sep 8, 2026, 1:21 PM",
        "September 8, 2026 at 1:21 PM",
        "2026-09-08T13:21:00-04:00",
        "2026-09-08T17:21:00Z",
    ],
)
def test_explicit_mail_dates_use_the_source_timezone(label):
    assert parse_received_at(label, "America/New_York") == datetime(2026, 9, 8, 17, 21, tzinfo=UTC)


def test_real_gmail_narrow_space_tooltip_and_full_weekday():
    expected = datetime(2026, 9, 11, 20, 44, tzinfo=UTC)
    assert parse_received_at("Fri, Sep 11, 2026, 4:44\u202fPM", "America/New_York") == expected
    assert parse_received_at("Friday, September 11, 2026, 4:44 PM", "America/New_York") == expected


def test_body_budget_eventually_reaches_every_row_despite_failures_or_missing_keys():
    rows = [{"id": str(index), "content_key": "text-links-v1:"} for index in range(50)]
    cache = {row["id"]: row | {"body_complete": True} for row in rows}
    attempts, visited = {}, set()
    for turn in range(7):
        chosen = select_body_ids(rows, cache, attempts, 8)
        assert len(chosen) == 8
        visited.update(chosen)
        # Record even failed reads: neither failure nor missing keys can monopolize a page.
        attempts.update({key: f"2026-09-12T00:0{turn}:00Z" for key in chosen})
    assert visited == set(cache)


def test_body_selection_deduplicates_rows_and_skips_verified_unchanged_content():
    row = {"id": "one", "content_key": "text-links-v1:123"}
    rows = [row, row, {"id": "", "content_key": ""}, {"id": "two"}]
    assert select_body_ids(rows, {}, {}, 8) == ["one", "two"]
    assert select_body_ids(rows, {"one": row | {"body_complete": True}}, {}, 8) == ["two"]
    assert select_body_ids(rows, {}, {}, 0) == []


def test_attempt_order_compares_instants_and_treats_invalid_timestamps_as_unread():
    rows = [{"id": "newer"}, {"id": "older"}, {"id": "invalid"}]
    attempts = {
        "newer": "2026-09-11T21:00:00-04:00",
        "older": "2026-09-12T00:00:00Z",
        "invalid": "not a timestamp",
    }
    assert select_body_ids(rows, {}, attempts, 3) == ["invalid", "older", "newer"]


@pytest.mark.parametrize(
    "label",
    [
        "",
        "Yesterday",
        "13:21",
        "Sep 8",
        "Sep 8, 2026",
        "2026-09-08",
        "Nov 1, 2026, 01:30",
        "Mar 8, 2026, 02:30",
    ],
)
def test_ambiguous_or_incomplete_mail_dates_remain_unknown(label):
    assert parse_received_at(label, "America/New_York") is None


def test_history_cursor_only_advances_after_committed_headers_and_known_pager():
    progress = {"next_page": 8, "unrelated": "retained"}
    for complete, older in ((False, True), (False, False), (True, None)):
        assert advance_history(progress, 8, headers_complete=complete, has_older=older) == progress
    assert advance_history(progress, 1, headers_complete=True, has_older=True) == progress
    assert advance_history(progress, 9, headers_complete=True, has_older=True) == progress
    assert advance_history(progress, 8, headers_complete=True, has_older=True) == progress | {
        "next_page": 9
    }
    assert advance_history(progress, 8, headers_complete=True, has_older=False) == progress | {
        "next_page": 2
    }
    assert progress["next_page"] == 8
    for invalid in (None, {}, {"next_page": True}, {"next_page": -1}, {"next_page": "8"}):
        assert history_page(invalid) == 2
