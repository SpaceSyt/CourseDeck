from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator


def now() -> datetime:
    return datetime.now(UTC)


def identity(provider: str, *parts: str) -> str:
    return ":".join(quote(part, safe="") for part in (provider, *parts))


class Outcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    AUTH_REQUIRED = "auth_required"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class Course(BaseModel):
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    external_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1)
    section: str | None = None
    teacher: str | None = None
    source_url: str | None = None
    raw_metadata: dict = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return identity(self.provider, self.external_id)


class Task(BaseModel):
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    external_id: str = Field(min_length=1, max_length=512)
    course_external_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1)
    description: str = ""
    due_at: datetime | None = None
    available_at: datetime | None = None
    closes_at: datetime | None = None
    url: str | None = None
    points_possible: float | None = None
    score: float | None = None
    submission_status: str = "unknown"
    graded: bool = False
    source_updated_at: datetime | None = None
    raw_data: dict = Field(default_factory=dict)

    @field_validator("due_at", "available_at", "closes_at", "source_updated_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Source timestamps must include a timezone")
            return value.astimezone(UTC)
        return None

    @property
    def id(self) -> str:
        return identity(self.provider, self.course_external_id, self.external_id)

    @property
    def course_id(self) -> str:
        return identity(self.provider, self.course_external_id)


class LocalState(BaseModel):
    hidden: bool = False
    dismissed: bool = False
    pinned: bool = False
    note: str = Field(default="", max_length=10000)
    priority: int = Field(default=0, ge=0, le=3)


class SyncResult(BaseModel):
    outcome: Outcome
    courses: list[Course] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    # True only after exhausting every page/course in a known full snapshot.
    complete: bool = False


class Settings(BaseModel):
    startup_sync: bool = True
    timezone: str = "America/New_York"
    show_completed: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone, e.g. America/New_York") from exc
        return value
