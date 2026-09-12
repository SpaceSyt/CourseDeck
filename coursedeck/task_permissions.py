"""Small deterministic command grammar for local Chat writes.

This is intentionally not a general natural-language classifier. Unsupported or
ambiguous requests remain read-only; model output cannot expand a grant.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import now


def explicit_deadline(raw, timezone):
    """Resolve an explicit clock time; never supply an omitted time of day."""
    zone = ZoneInfo(timezone)
    local_day = now().astimezone(zone).date()
    english = re.fullmatch(r"(today|tomorrow)(?: at)? (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw, re.I)
    chinese = re.fullmatch(r"(今天|明天|后天)(上午|下午|晚上)?(\d{1,2}):(\d{2})", raw)
    if english or chinese:
        hit = english or chinese
        days = {"today": 0, "tomorrow": 1, "今天": 0, "明天": 1, "后天": 2}[hit[1].lower()]
        if english:
            hour, minute, period = int(hit[2]), int(hit[3] or 0), (hit[4] or "").lower()
        else:
            hour, minute = int(hit[3]), int(hit[4])
            period = {"上午": "am", "下午": "pm", "晚上": "pm"}.get(hit[2], "")
        if period:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if period == "pm" else 0)
        raw = f"{local_day + timedelta(days=days)}T{hour:02}:{minute:02}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", raw):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            first, second = value.replace(tzinfo=zone, fold=0), value.replace(tzinfo=zone, fold=1)
            # Ambiguous/nonexistent local clock times need an explicit offset.
            if first.utcoffset() != second.utcoffset():
                return None
            value = first
        return value.isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class EditIntent:
    target: str
    fields: dict
    undo: bool = False


def parse_intent(message, timezone="UTC"):
    text = message.strip()
    # Quotes may contain task names or note values, but cannot supply the command.
    if not text or text[0] in "\"'“‘`>":
        return None
    text = re.sub(r"^(?:please\s+|请(?:帮我)?|帮我)", "", text, flags=re.I).strip()
    if re.match(r"(?:不要|不想|别|不能|是否|能否|怎么|如何|假如|如果)", text):
        return None
    # A note's literal value may itself contain questions, negation or commands.
    # Keep its punctuation; it is data for this one field, never another grant.
    note = re.fullmatch(r"(?:set|update)\s+(.+?)(?:'s)?\s+note\s+to\s+(.+)", text, re.I)
    if not note:
        note = re.fullmatch(r"(?:把|将)?(.+?)(?:的)?备注(?:改为|设为|改成)\s*(.+)", text)
    if note:
        literal = note[2]
        if (literal[:1], literal[-1:]) in {('"', '"'), ("“", "”"), ("'", "'")}:
            literal = literal[1:-1]
        return EditIntent(note[1], {"note": literal})
    text = text.rstrip(".!。！").strip()
    if re.search(
        r"\b(?:not|never|don't|dont|cannot|can't|should|would|could|why|how|if)\b|"
        r"不要|不想|别|不能|是否|能否|怎么|如何|假如|如果|[?？]",
        text,
        re.I,
    ):
        return None
    match = re.fullmatch(
        r"(?:mark|set)\s+(.+?)\s+(?:as\s+)?(done|complete|completed|open)", text, re.I
    )
    if not match:
        match = re.fullmatch(
            r"(?:把|将)?(.+?)(?:标记为|标为|设为|设置为)(完成|已完成|未完成|待办)", text
        )
    if not match:
        match = re.fullmatch(r"(?:标记|设置)(.+?)为(完成|已完成|未完成|待办)", text)
    if match:
        status = "open" if match[2].lower() in {"open", "未完成", "待办"} else "done"
        return EditIntent(match[1], {"completion": status})
    match = re.fullmatch(r"reopen\s+(.+)", text, re.I)
    if match:
        return EditIntent(match[1], {"completion": "open"})
    match = re.fullmatch(r"(?:把|将)?(.+?)划掉", text)
    if match:
        return EditIntent(match[1], {"completion": "done"})
    match = re.fullmatch(r"(?:undo|撤销)\s*(.+)", text, re.I)
    if match:
        return EditIntent(match[1], {}, undo=True)
    match = re.fullmatch(
        r"restore\s+(.+?)(?:'s)?\s+(?:source\s+)?(?:deadline|due date)", text, re.I
    )
    if not match:
        match = re.fullmatch(r"恢复(.+?)(?:的)?(?:原始|来源)?截止时间", text)
    if match:
        return EditIntent(match[1], {"restore_due_date": True})
    match = re.fullmatch(
        r"(?:change|set|update|reschedule)\s+(.+?)(?:'s)?\s+"
        r"(?:deadline|due date|ddl)\s+to\s+(.+)",
        text,
        re.I,
    )
    if not match:
        match = re.fullmatch(
            r"(?:把|将)?(.+?)(?:的)?\s*(?:ddl|截止时间|截止日期)\s*"
            r"(?:改到|改成|改为|设为|设置为|推迟到|提前到)\s*(.+)",
            text,
            re.I,
        )
    if not match:
        match = re.fullmatch(r"reschedule\s+(.+?)\s+to\s+(.+)", text, re.I)
    if match:
        raw = match[2].strip()
        if raw.lower() in {"none", "no deadline", "无", "没有截止时间"}:
            return EditIntent(match[1], {"due_at": None})
        try:
            value = explicit_deadline(raw, timezone)
            return EditIntent(match[1], {"due_at": value}) if value else None
        except (ValueError, KeyError):
            return None
    return None


def normalized(value):
    return re.sub(r"\s+", " ", value.strip().strip("\"'“”‘’")).casefold()


def resolve_target(intent, tasks, selected_id=None):
    target = normalized(intent.target)
    if target in {
        "this",
        "this task",
        "it",
        "这个",
        "它",
        "此任务",
        "这个任务",
        "这项作业",
        "这份作业",
    }:
        return next((task["id"] for task in tasks if task["id"] == selected_id), None)
    matches = [
        task["id"]
        for task in tasks
        if target in {normalized(task["id"]), normalized(task["title"])}
    ]
    return matches[0] if len(matches) == 1 else None
