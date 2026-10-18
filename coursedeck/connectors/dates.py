from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def source_date(value: str | None, timezone: str | None = None) -> datetime | None:
    """Parse explicit ISO timestamps; require a source zone for local wall times."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    if not timezone:
        raise ValueError("Source timezone missing")
    zone = ZoneInfo(timezone)
    first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Ambiguous or nonexistent DST wall time")
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed:
        raise ValueError("Nonexistent wall time")
    return first.astimezone(UTC)
