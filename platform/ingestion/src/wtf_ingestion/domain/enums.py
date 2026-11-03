from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    EARNINGS_RELEASE = "earnings_release"
    ECONOMIC_RELEASE = "economic_release"


class EventStatus(StrEnum):
    ESTIMATED = "estimated"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    MOVED = "moved"
    CANCELLED = "cancelled"
    PUBLISHED = "published"
    UNKNOWN = "unknown"


class Session(StrEnum):
    BMO = "BMO"
    AMC = "AMC"
    DURING_MARKET = "DURING_MARKET"
    UNKNOWN = "UNKNOWN"


class TimePrecision(StrEnum):
    EXACT = "exact"
    DATE_ONLY = "date_only"
    UNKNOWN = "unknown"


class RunState(StrEnum):
    RUNNING = "RUNNING"
    HEALTHY = "HEALTHY"
    DELAYED = "DELAYED"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    FAILED = "FAILED"
    VALID_EMPTY = "VALID_EMPTY"
    SUSPICIOUS_EMPTY = "SUSPICIOUS_EMPTY"
    NEVER_RUN = "NEVER_RUN"


class CoverageState(StrEnum):
    CONFIRMED = "confirmed"
    ANNOUNCED_DATE_ONLY = "announced_date_only"
    ESTIMATED = "estimated"
    NOT_YET_ANNOUNCED = "not_yet_announced"
    SOURCE_UNAVAILABLE = "source_unavailable"
    UNSUPPORTED = "unsupported"
