"""Shared, conservative task interpretation; source strings remain unchanged.

Connectors may declare ``raw_data.field_availability[field]`` explicitly. Legacy
``unavailable_fields`` always wins; cached values must not disguise a failed read.
Undeclared null fields are unknown, not proof that the platform has no value.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class SourceState(StrEnum):
    OPEN = "open"
    DONE = "done"
    UNKNOWN = "unknown"


class SourceAvailability(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    UNCONFIRMED = "unconfirmed"


class FieldAvailability(StrEnum):
    KNOWN = "known"
    EMPTY = "empty"
    UNREAD = "unread"
    UNKNOWN = "unknown"


DONE_STATUSES = frozenset({"submitted", "completed", "returned", "graded"})
OPEN_STATUSES = frozenset(
    {
        "new",
        "assigned",
        "missing",
        "incomplete",
        "not_submitted",
        "unsubmitted",
        "not_started",
        "open",
        "in_progress",
        "started",
        "reopened",
        "returned_for_revision",
        "resubmission_required",
    }
)
TASK_FIELDS = (
    "due_at",
    "available_at",
    "closes_at",
    "description",
    "submission_status",
    "score",
    "graded",
    "points_possible",
)


def normalize_status(value: Any) -> SourceState:
    # Exact connector-normalized tokens only. Do not guess from translated labels,
    # partial words, grades, or merely reaching an assignment's deadline.
    if isinstance(value, str) and value in DONE_STATUSES:
        return SourceState.DONE
    if isinstance(value, str) and value in OPEN_STATUSES:
        return SourceState.OPEN
    return SourceState.UNKNOWN


def source_availability(task: Mapping) -> SourceAvailability:
    explicit = task.get("source_availability")
    if explicit in set(SourceAvailability):
        return SourceAvailability(explicit)
    return (
        SourceAvailability.MISSING
        if task.get("missing_count", 0) > 0
        else SourceAvailability.PRESENT
    )


def field_availability(task: Mapping, field: str) -> FieldAvailability:
    raw = task.get("raw_data") or {}
    if not isinstance(raw, Mapping):
        raw = {}
    if field in (raw.get("unavailable_fields") or []):
        return FieldAvailability.UNREAD
    if source_availability(task) != SourceAvailability.PRESENT:
        return FieldAvailability.UNREAD
    declarations = raw.get("field_availability") or {}
    declared = declarations.get(field) if isinstance(declarations, Mapping) else None
    if declared in {FieldAvailability.UNREAD, FieldAvailability.UNKNOWN}:
        return FieldAvailability(declared)
    value = task.get(field)
    if field == "submission_status":
        if task.get("source_status_known") is False or value in (None, "", "unknown"):
            return FieldAvailability.UNKNOWN
    if value is not None and value != "":
        return FieldAvailability.KNOWN
    if declared == FieldAvailability.EMPTY:
        return FieldAvailability.EMPTY
    return FieldAvailability.UNKNOWN


def unread_fields(raw: Mapping) -> set[str]:
    """Fields explicitly not observed must retain any previously read value."""
    unavailable = raw.get("unavailable_fields") or []
    fields = set(unavailable) if isinstance(unavailable, (list, tuple, set)) else set()
    declarations = raw.get("field_availability") or {}
    if isinstance(declarations, Mapping):
        fields.update(
            key
            for key, value in declarations.items()
            if value in (FieldAvailability.UNREAD, FieldAvailability.UNKNOWN)
        )
    return fields & set(TASK_FIELDS)


def project_task_state(task: Mapping) -> dict:
    availability = source_availability(task)
    fields = {field: field_availability(task, field).value for field in TASK_FIELDS}
    known = fields["submission_status"] == FieldAvailability.KNOWN
    state = normalize_status(task.get("submission_status")) if known else SourceState.UNKNOWN
    local = task.get("local") or {}
    override = local.get("completion_override")
    completed = (
        override == "done"
        if override in {"done", "open"}
        else bool(local.get("dismissed") or state == SourceState.DONE)
    )
    return {
        "source_availability": availability.value,
        "source_status_known": known,
        "source_state": state.value,
        "field_availability": fields,
        "is_completed": completed,
    }


def contract() -> dict:
    """Exported to the frontend; CI checks the generated artifact is current."""
    return {
        "done_statuses": sorted(DONE_STATUSES),
        "open_statuses": sorted(OPEN_STATUSES),
        "fields": list(TASK_FIELDS),
        "source_states": [item.value for item in SourceState],
        "source_availability": [item.value for item in SourceAvailability],
        "field_availability": [item.value for item in FieldAvailability],
    }
