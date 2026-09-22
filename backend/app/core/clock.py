"""Time acquisition only; no freshness policy."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.utcoffset() != timedelta(0):
            raise ValueError("FixedClock requires an aware UTC instant")

    def now(self) -> datetime:
        return self.instant
