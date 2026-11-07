"""Time is injected through the Clock protocol so that every timestamp is testable."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock. This is the only place that reads the wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A deterministic clock for tests. It only moves when told to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("FixedClock cannot move backwards")
        self._now = self._now + delta
