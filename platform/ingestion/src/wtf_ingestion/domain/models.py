from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from wtf_ingestion.domain.enums import EventStatus, EventType, Session, TimePrecision
from wtf_ingestion.safety import validate_public_url


def utc_now() -> datetime:
    return datetime.now(UTC)


class EventCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_fields: ClassVar[tuple[str, ...]] = (
        "event_type",
        "ticker",
        "exchange",
        "company_name",
        "cik",
        "title",
        "event_date",
        "scheduled_start",
        "source_timezone",
        "session",
        "fiscal_period",
        "status",
        "time_precision",
        "source_id",
        "source_event_id",
        "source_url",
        "parser_version",
    )

    event_type: EventType
    ticker: str | None = None
    exchange: str | None = None
    company_name: str | None = None
    cik: str | None = None
    title: str = Field(min_length=1, max_length=500)
    event_date: date | None = None
    scheduled_start: datetime | None = None
    source_timezone: str | None = None
    session: Session = Session.UNKNOWN
    fiscal_period: str | None = Field(default=None, max_length=100)
    status: EventStatus
    time_precision: TimePrecision
    source_id: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(min_length=1, max_length=300)
    source_url: HttpUrl
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    collected_at: datetime = Field(default_factory=utc_now)
    parser_version: str = Field(min_length=1, max_length=100)
    evidence_locator: str | None = Field(default=None, max_length=500)
    raw_payload_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_url")
    @classmethod
    def public_source_url(cls, value: HttpUrl) -> HttpUrl:
        validate_public_url(str(value))
        return value

    @field_validator("source_timezone")
    @classmethod
    def timezone_exists(cls, value: str | None) -> str | None:
        if value:
            ZoneInfo(value)
        return value

    @field_validator("ticker", "exchange")
    @classmethod
    def normalize_upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("cik")
    @classmethod
    def normalize_cik(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = value.strip().lstrip("0") or "0"
        if not digits.isdigit() or len(digits) > 10:
            raise ValueError("CIK must contain at most 10 digits")
        return digits.zfill(10)

    @field_validator("scheduled_start", "source_published_at", "source_updated_at", "collected_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_time_and_identity(self) -> EventCandidate:
        if self.time_precision == TimePrecision.EXACT and self.scheduled_start is None:
            raise ValueError("exact events require scheduled_start")
        if self.event_date is None and self.scheduled_start is None:
            raise ValueError("event_date or scheduled_start is required")
        if self.event_type == EventType.EARNINGS_RELEASE and not (self.ticker or self.cik):
            raise ValueError("earnings events require ticker or CIK")
        if self.scheduled_start and self.event_date:
            zone = ZoneInfo(self.source_timezone) if self.source_timezone else UTC
            if self.scheduled_start.astimezone(zone).date() != self.event_date:
                raise ValueError("event_date must equal the source-local scheduled date")
        if self.event_date is None and self.scheduled_start:
            self.event_date = self.scheduled_start.date()
        return self

    def material_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include=set(self.material_fields))

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(self.material_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def canonical_key(self) -> str:
        raw = f"{self.source_id}\x1f{self.source_event_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SourceRecord(BaseModel):
    key: str
    name: str
    owner: str
    source_type: str
    base_url: HttpUrl
    permission_status: str
    access_method: str
    rate_policy: str
    freshness_hours: int = 36
    live_enabled: bool = False
    fixture_only: bool = True
    reviewed_at: date
    parser_version: str
    limitations: str


class FeedRecord(BaseModel):
    schema_version: str = "0.2.0"
    event_id: UUID
    revision_number: int
    event_type: str
    ticker: str | None
    exchange: str | None
    company_name: str | None
    cik: str | None
    title: str
    event_date: date | None
    scheduled_start: datetime | None
    source_timezone: str | None = None
    session: str
    fiscal_period: str | None
    status: str
    time_precision: str
    source_id: str
    source_event_id: str
    source_url: str
    collected_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    content_hash: str
    parser_version: str
    date_basis: Literal["announced", "estimated", "official_schedule"] = "announced"
    estimate_method: str | None = None
    estimate_basis_date: date | None = None
    estimate_basis_url: str | None = None
    estimate_confidence: str | None = None
