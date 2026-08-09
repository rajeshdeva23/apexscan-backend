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
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.config import Settings
from app.market_engine.context import MarketState, SessionContext

_TIME_FORMAT = "%H:%M"
_SATURDAY = 5
_SUNDAY = 6


def _parse_time(value: str) -> time:
    """Parse an ``HH:MM`` exchange-local time string."""
    return datetime.strptime(value.strip(), _TIME_FORMAT).replace(tzinfo=None).time()


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


class TradingCalendar:
    """A broker-neutral trading calendar: weekends and configured holidays are closed."""

    def __init__(
        self,
        *,
        holidays: Iterable[date] = (),
        weekend_days: Iterable[int] = (_SATURDAY, _SUNDAY),
    ) -> None:
        """Build the calendar from configured, deterministic data (no remote fetch).

        Args:
            holidays: Exchange holiday dates that are non-trading days.
            weekend_days: ``date.weekday()`` values treated as non-trading (Sat/Sun).
        """
        self._holidays = frozenset(holidays)
        self._weekend_days = frozenset(weekend_days)

    def is_trading_day(self, trading_date: date) -> bool:
        """Return whether the exchange is open for trading on the given date."""
        if trading_date.weekday() in self._weekend_days:
            return False
        return trading_date not in self._holidays


class MarketSessionClassifier:
    """Classifies a canonical UTC instant into exchange-local session facts."""

    def __init__(
        self,
        *,
        schedule: SessionSchedule,
        calendar: TradingCalendar,
        exchange_timezone: str,
    ) -> None:
        """Wire the classifier to a schedule, calendar, and exchange timezone.

        Args:
            schedule: The validated session boundaries.
            calendar: The trading calendar.
            exchange_timezone: The IANA timezone name (e.g. "Asia/Kolkata").
        """
        self._schedule = schedule
        self._calendar = calendar
        self._timezone_name = exchange_timezone
        self._timezone = ZoneInfo(exchange_timezone)

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
        if not self._calendar.is_trading_day(trading_date):
            return MarketState.HOLIDAY
        if halt_active:
            return MarketState.EMERGENCY_HALT
        return self._schedule.phase_for(local_time)
