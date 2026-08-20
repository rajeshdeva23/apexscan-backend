"""Deterministic market-session layer for the Market Engine (docs/06 §7-§8).

Given a canonical (UTC) instant plus a broker-neutral trading calendar, session
schedule, exchange timezone, and an optional external halt fact, this module
classifies exactly one market phase and the exchange-local trading date. It is a
pure classifier — no wall-clock reads, no background scheduler, no provider
knowledge — so the same inputs always yield the same session facts (docs/06
§1.4, §7.3). Candles, historical context, and features are out of scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.config import Settings
from app.market_engine.context import MarketState, SessionContext
from app.market_engine.historical.calendar_window import CalendarCoverage

_TIME_FORMAT = "%H:%M"
_SATURDAY = 5
_SUNDAY = 6


def _parse_time(value: str) -> time:
    """Parse an ``HH:MM`` exchange-local time string."""
    return datetime.strptime(value.strip(), _TIME_FORMAT).replace(tzinfo=None).time()


@dataclass(frozen=True, slots=True)
class TradingInterval:
    """One exchange-local ``[start, end)`` live-market interval.

    The single canonical, broker-neutral, immutable per-interval unit the shared
    bucket algorithm operates on. Both the default schedule and each per-date
    override live interval collapse to this one type (ADR-011 multi-interval
    addendum MI2). A date with regular hours or a single special block carries
    exactly one interval; a disjoint multi-block session carries several.

    Attributes:
        start: The exchange-local start of the live interval.
        end: The exchange-local end of the live interval.
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        """Reject a non-increasing interval, failing fast at construction (MI2)."""
        if self.start >= self.end:
            raise ValueError("trading interval requires start < end")


class SessionSchedule(BaseModel):
    """Immutable, validated exchange-local session boundaries (docs/06 §8).

    Boundaries partition a trading day into half-open ``[start, end)`` phases and
    must be strictly increasing. Times are exchange-local wall-clock times; the
    exact NSE values are configuration, not embedded assumptions (docs/06 §8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pre_open_start: time
    opening_auction_start: time
    regular_open: time
    regular_close: time
    closing_end: time

    @model_validator(mode="after")
    def _validate_strictly_increasing(self) -> SessionSchedule:
        boundaries = (
            self.pre_open_start,
            self.opening_auction_start,
            self.regular_open,
            self.regular_close,
            self.closing_end,
        )
        pairs = zip(boundaries, boundaries[1:], strict=False)
        if not all(earlier < later for earlier, later in pairs):
            raise ValueError("session boundaries must be strictly increasing")
        return self

    def phase_for(self, moment: time) -> MarketState:
        """Return the market phase for an exchange-local time on a trading day."""
        if moment < self.pre_open_start:
            return MarketState.MARKET_CLOSED
        if moment < self.opening_auction_start:
            return MarketState.PRE_OPEN
        if moment < self.regular_open:
            return MarketState.OPENING_AUCTION
        if moment < self.regular_close:
            return MarketState.LIVE_SESSION
        if moment < self.closing_end:
            return MarketState.CLOSING_SESSION
        return MarketState.MARKET_CLOSED

    @property
    def bounds(self) -> TradingInterval:
        """Return the ``[regular_open, regular_close)`` interval of this schedule."""
        return TradingInterval(start=self.regular_open, end=self.regular_close)


@dataclass(frozen=True, slots=True)
class TradingSessionOverride:
    """Per-date exchange-local live intervals for an exceptional OPEN session.

    Broker-neutral and immutable: it carries the ordered live-market intervals that
    historical intraday reconstruction needs for a date whose hours differ from the
    default schedule (ADR-011 multi-interval addendum MI3). A date may declare one
    interval (a single special block) or several disjoint blocks (e.g. a session
    interrupted by a mid-day closure). Pre-open, auction, and closing phases are
    deliberately not modelled — reconstruction does not consume them.

    Intervals are validated, never silently sorted or merged, so malformed
    authoritative data fails fast at the source (MI4–MI6).

    Attributes:
        trading_date: The exchange-local date these intervals apply to.
        live_intervals: The chronologically ordered, non-overlapping, strictly
            gapped live-market intervals (``len >= 1``).
    """

    trading_date: date
    live_intervals: tuple[TradingInterval, ...]

    def __post_init__(self) -> None:
        """Reject malformed interval sets, failing fast at construction (MI3–MI6)."""
        if not self.live_intervals:
            raise ValueError("session override requires at least one live interval")
        for earlier, later in zip(self.live_intervals, self.live_intervals[1:], strict=False):
            if earlier.end >= later.start:
                raise ValueError(
                    "live_intervals must be chronologically ordered, non-overlapping, "
                    "and separated by a strictly positive gap"
                )

    @classmethod
    def continuous(cls, trading_date: date, start: time, end: time) -> TradingSessionOverride:
        """Build a one-interval override spanning a single continuous block.

        Args:
            trading_date: The exchange-local date the interval applies to.
            start: The exchange-local start of the single live interval.
            end: The exchange-local end of the single live interval.

        Returns:
            A :class:`TradingSessionOverride` carrying exactly one interval.
        """
        return cls(
            trading_date=trading_date, live_intervals=(TradingInterval(start=start, end=end),)
        )


class EffectiveSchedule:
    """Resolves the effective live intervals for a trading date (ADR-011 addendum MI16).

    Calendar-agnostic: it maps a date to its per-date override's live intervals when
    one exists, otherwise to a single-element tuple of the canonical default
    schedule's bounds. A special date never mutates the default and never leaks its
    hours to adjacent dates.
    """

    def __init__(
        self,
        *,
        default: SessionSchedule,
        overrides: Iterable[TradingSessionOverride] = (),
    ) -> None:
        """Wire the effective schedule to a default and per-date overrides.

        Args:
            default: The canonical schedule used for any date without an override.
            overrides: Per-date session-hour overrides; duplicate dates are rejected.

        Raises:
            ValueError: If two overrides share the same ``trading_date`` (M13).
        """
        self._default = default
        by_date: dict[date, TradingSessionOverride] = {}
        for override in overrides:
            if override.trading_date in by_date:
                iso = override.trading_date.isoformat()
                raise ValueError(f"duplicate session override for trading date {iso}")
            by_date[override.trading_date] = override
        self._overrides = by_date

    @property
    def default(self) -> SessionSchedule:
        """Return the canonical default schedule."""
        return self._default

    def has_override(self, trading_date: date) -> bool:
        """Return whether a per-date session-hour override exists for the date."""
        return trading_date in self._overrides

    def intervals_for(self, trading_date: date) -> tuple[TradingInterval, ...]:
        """Return the ordered effective live intervals for the date (MI16).

        Args:
            trading_date: The exchange-local date to resolve.

        Returns:
            The override's live intervals if one exists, else a single-element
            tuple of the default schedule's bounds.
        """
        override = self._overrides.get(trading_date)
        if override is not None:
            return override.live_intervals
        return (self._default.bounds,)

    def envelope_for(self, trading_date: date) -> TradingInterval:
        """Return the whole-session envelope ``[first start, last end)`` for the date.

        For a multi-interval special date the envelope spans the first interval's
        start to the last interval's end (the intervening closure yields no source
        candles); for any other date it is the default bounds. The envelope is used
        only for whole-session (daily) identity, never for intraday capacity or
        completeness (ADR-011 addendum MI10/MI12).

        Args:
            trading_date: The exchange-local date to resolve.

        Returns:
            The single :class:`TradingInterval` spanning the date's live envelope.
        """
        override = self._overrides.get(trading_date)
        if override is None:
            return self._default.bounds
        return TradingInterval(
            start=override.live_intervals[0].start, end=override.live_intervals[-1].end
        )


class TradingCalendar:
    """A broker-neutral trading calendar: weekends and closed dates are non-trading.

    Exceptional OPEN sessions (e.g. a weekend-open) are represented explicitly and take
    precedence over the weekend/closed rules (ADR-011 addendum M5). An empty
    ``open_sessions`` preserves the historical weekend/holiday-only behaviour exactly.
    """

    def __init__(
        self,
        *,
        holidays: Iterable[date] = (),
        closed_dates: Iterable[date] = (),
        open_sessions: Iterable[date] = (),
        weekend_days: Iterable[int] = (_SATURDAY, _SUNDAY),
    ) -> None:
        """Build the calendar from configured, deterministic data (no remote fetch).

        Args:
            holidays: Exchange closure dates (compat alias folded into closed dates).
            closed_dates: General exchange-closure dates that are non-trading days.
            open_sessions: Exceptional OPEN dates that trade despite weekend/closure.
            weekend_days: ``date.weekday()`` values treated as non-trading (Sat/Sun).

        Raises:
            ValueError: If any date is both an OPEN session and a closed date (M4).
        """
        self._closed_dates = frozenset(holidays) | frozenset(closed_dates)
        self._open_sessions = frozenset(open_sessions)
        self._weekend_days = frozenset(weekend_days)
        conflict = self._closed_dates & self._open_sessions
        if conflict:
            offending = ", ".join(day.isoformat() for day in sorted(conflict))
            raise ValueError(f"dates cannot be both open and closed: {offending}")

    @property
    def holidays(self) -> frozenset[date]:
        """Return the closed-date set (compat alias for the former holiday set)."""
        return self._closed_dates

    @property
    def closed_dates(self) -> frozenset[date]:
        """Return the exchange-closure dates that are non-trading days."""
        return self._closed_dates

    @property
    def open_sessions(self) -> frozenset[date]:
        """Return the exceptional OPEN dates that trade despite weekend/closure."""
        return self._open_sessions

    def is_trading_day(self, trading_date: date) -> bool:
        """Return whether the exchange is open for trading on the given date (M5)."""
        if trading_date in self._open_sessions:
            return True
        if trading_date.weekday() in self._weekend_days:
            return False
        return trading_date not in self._closed_dates


class MarketSessionClassifier:
    """Classifies a canonical UTC instant into exchange-local session facts."""

    def __init__(
        self,
        *,
        schedule: SessionSchedule,
        calendar: TradingCalendar,
        exchange_timezone: str,
        coverage: CalendarCoverage | None = None,
    ) -> None:
        """Wire the classifier to a schedule, calendar, exchange timezone, and coverage.

        Args:
            schedule: The validated session boundaries.
            calendar: The trading calendar.
            exchange_timezone: The IANA timezone name (e.g. "Asia/Kolkata").
            coverage: The inclusive date range the calendar is authoritative over
                (ADR-011 live out-of-coverage addendum LC5/LC6). When provided,
                :meth:`classify` returns ``CALENDAR_UNAVAILABLE`` for any date outside
                it, checked before the trading-day/halt/phase logic. ``None`` (the
                default) preserves the legacy no-coverage-check behaviour for the
                disabled/no-live path, which carries no live data.
        """
        self._schedule = schedule
        self._calendar = calendar
        self._timezone_name = exchange_timezone
        self._timezone = ZoneInfo(exchange_timezone)
        self._coverage = coverage

    @classmethod
    def from_settings(cls, settings: Settings) -> MarketSessionClassifier:
        """Build a classifier from validated application settings."""
        schedule = SessionSchedule(
            pre_open_start=_parse_time(settings.nse_pre_open_start),
            opening_auction_start=_parse_time(settings.nse_opening_auction_start),
            regular_open=_parse_time(settings.nse_regular_open),
            regular_close=_parse_time(settings.nse_regular_close),
            closing_end=_parse_time(settings.nse_closing_end),
        )
        holidays = [
            date.fromisoformat(entry.strip())
            for entry in settings.nse_holidays.split(",")
            if entry.strip()
        ]
        return cls(
            schedule=schedule,
            calendar=TradingCalendar(holidays=holidays),
            exchange_timezone=settings.exchange_timezone,
        )

    def classify(self, instant: datetime, *, halt_active: bool = False) -> SessionContext:
        """Classify a canonical UTC instant into broker-neutral session facts.

        Args:
            instant: A timezone-aware UTC instant (canonical event time).
            halt_active: Whether an external source reports an emergency halt.

        Returns:
            The :class:`SessionContext` (trading date, market phase, timezone).
        """
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("session classification requires a timezone-aware instant")
        local = instant.astimezone(self._timezone)
        trading_date = local.date()
        state = self._state_for(trading_date, local.time(), halt_active=halt_active)
        return SessionContext(
            trading_date=trading_date,
            market_state=state,
            exchange_timezone=self._timezone_name,
        )

    def _state_for(self, trading_date: date, local_time: time, *, halt_active: bool) -> MarketState:
        if self._coverage is not None and not self._coverage.contains(trading_date):
            return MarketState.CALENDAR_UNAVAILABLE
        if not self._calendar.is_trading_day(trading_date):
            return MarketState.HOLIDAY
        if halt_active:
            return MarketState.EMERGENCY_HALT
        return self._schedule.phase_for(local_time)
