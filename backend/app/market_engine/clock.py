"""Broker-independent, injectable clock abstraction for the Market Engine.

All engine time is handled in UTC internally (docs/06 §12.7, §28.25; docs/02
§4.1). The clock is injected so the engine never calls ``datetime.now`` on its
own hot path, which keeps context building deterministic and replay-safe
(docs/06 §1.4, §28.8; docs/11 §2.9). Exchange-local (e.g. IST) conversion is a
later, edge concern and is deliberately not implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """A source of the current instant as a timezone-aware UTC datetime."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` normalised to timezone-aware UTC, rejecting naive input."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("clock time must be timezone-aware")
    return moment.astimezone(UTC)


class SystemClock:
    """The production clock; the only component permitted to read wall-clock time."""

    def now(self) -> datetime:
        """Return the current wall-clock instant in UTC."""
        return datetime.now(UTC)


@dataclass(slots=True)
class ManualClock:
    """A deterministic, injectable clock for tests and deterministic replay.

    The time never advances on its own; it changes only through :meth:`set` or
    :meth:`advance`, so replaying the same script yields identical timestamps.
    """

    _current: datetime

    def __post_init__(self) -> None:
        """Normalise the initial instant to timezone-aware UTC."""
        self._current = _as_utc(self._current)

    def now(self) -> datetime:
        """Return the current scripted instant."""
        return self._current

    def set(self, moment: datetime) -> None:
        """Set the clock to an explicit timezone-aware instant (normalised to UTC)."""
        self._current = _as_utc(moment)

    def advance(self, delta: timedelta) -> None:
        """Advance the clock by ``delta``."""
        self._current = self._current + delta
