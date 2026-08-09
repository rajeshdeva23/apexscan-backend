"""Broker-neutral, immutable timeframe value object for candle aggregation.

A ``Timeframe`` is either an intraday duration (e.g. 1m, 5m, 7m, 15m) or the
whole regular trading session. It carries no strategy meaning and no
timeframe-specific behaviour: the candle engine's aggregation is generic over
any valid duration (docs/06 §13, §27; ADR-005 design review).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class TimeframeKind(StrEnum):
    """Whether a timeframe is a fixed intraday duration or the whole session."""

    INTRADAY = "intraday"
    SESSION = "session"


@dataclass(frozen=True, slots=True, order=True)
class Timeframe:
    """An immutable, hashable candle timeframe (intraday duration or session).

    Attributes:
        kind: Whether this is an intraday or whole-session timeframe.
        duration: The intraday bucket width; ``None`` for a session timeframe.
    """

    kind: TimeframeKind
    duration: timedelta | None = None

    def __post_init__(self) -> None:
        """Validate the kind/duration combination, failing fast on invalid input."""
        if self.kind is TimeframeKind.INTRADAY:
            if self.duration is None or self.duration <= timedelta(0):
                raise ValueError("an intraday timeframe requires a strictly positive duration")
        elif self.duration is not None:
            raise ValueError("a session timeframe must not carry a duration")

    @classmethod
    def minutes(cls, count: int) -> Timeframe:
        """Build an intraday timeframe of ``count`` minutes (must be positive)."""
        if count <= 0:
            raise ValueError("timeframe minutes must be a positive integer")
        return cls(kind=TimeframeKind.INTRADAY, duration=timedelta(minutes=count))

    @classmethod
    def session(cls) -> Timeframe:
        """Build the whole-regular-session timeframe."""
        return cls(kind=TimeframeKind.SESSION)

    @property
    def is_session(self) -> bool:
        """Return whether this timeframe spans the whole regular session."""
        return self.kind is TimeframeKind.SESSION

    @property
    def label(self) -> str:
        """Return a stable, human-readable identifier for ordering and display."""
        if self.duration is None:
            return "session"
        return f"{int(self.duration.total_seconds())}s"
