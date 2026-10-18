"""Deterministic Gmail cache decisions, independent of browser navigation."""

import re
from datetime import datetime

from .connectors.dates import source_date

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        1,
    )
    for name in names
}


def needs_body_refresh(row, cached):
    """A reader-version prefix alone is not evidence that the thread is unchanged."""
    if not cached or not cached.get("body_complete"):
        return True
    content_key = (row.get("content_key") or "").strip()
    if not content_key or content_key.endswith(":"):
        return True
    return any(
        cached.get(field, "") != row.get(field, "")
        for field in ("content_key", "snippet", "date_label", "subject")
    )


def parse_received_at(date_label, timezone):
    """Parse an explicit full date and clock time; never infer a year or midnight.

    Gmail's English tooltip is supported alongside explicit ISO timestamps. Other
    locales and ambiguous DST wall times remain unknown for the caller to expose.
    """
    text = (date_label or "").strip().replace("\u202f", " ").replace("\xa0", " ")
    if not text:
        return None
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?", text
    ):
        try:
            return source_date(text, timezone)
        except (ValueError, KeyError):
            return None
    text = re.sub(
        r"^(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|"
        r"Sat(?:urday)?|Sun(?:day)?),?\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+at\s+", ", ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    match = re.fullmatch(
        r"([A-Za-z]+) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2})(?::(\d{2}))?(?: (AM|PM))?",
        text,
        re.I,
    )
    if not match or match[1].lower() not in _MONTHS:
        return None
    hour = int(match[4])
    if match[7]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match[7].upper() == "PM" else 0)
    try:
        value = datetime(
            int(match[3]),
            _MONTHS[match[1].lower()],
            int(match[2]),
            hour,
            int(match[5]),
            int(match[6] or 0),
        )
        return source_date(value.isoformat(), timezone)
    except (ValueError, KeyError):
        return None


def select_body_ids(rows, cached_by_id, attempts, limit):
    """Fair bounded body reads for one visible page, keyed by raw thread IDs.

    Persist an attempt timestamp before each selected read, including failed reads.
    A repeatedly unreadable first row must not permanently starve the rest of a page.
    """
    if limit <= 0:
        return []
    candidates, seen = [], set()
    for index, row in enumerate(rows):
        key = row.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        if needs_body_refresh(row, cached_by_id.get(key)):
            try:
                stamp = datetime.fromisoformat(attempts[key].replace("Z", "+00:00"))
                attempted = stamp.timestamp() if stamp.tzinfo is not None else float("-inf")
            except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
                attempted = float("-inf")
            candidates.append((attempted, index, key))
    return [key for _, _, key in sorted(candidates)[:limit]]


def history_page(progress):
    """Page one is always refreshed separately before bounded history work."""
    value = (progress or {}).get("next_page", 2)
    return value if type(value) is int and value >= 2 else 2


def advance_history(progress, page, *, headers_complete, has_older):
    """Commit a page only after every identifiable header is safely cached.

    Body failures are tracked independently and must be retried on later rotations.
    Unknown pagination is not an observed end. Page numbers are positions, so the
    cursor represents rotating coverage, never a proof of a complete mailbox.
    """
    result = dict(progress or {})
    if (
        type(page) is not int
        or page < 2
        or page != history_page(progress)
        or not headers_complete
        or type(has_older) is not bool
    ):
        return result
    result["next_page"] = page + 1 if has_older else 2
    return result
