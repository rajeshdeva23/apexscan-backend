"""Canonical broker-neutral historical OHLCV models (DECOUPLING PHASE F).

Immutable daily bars and series for durable multi-day history. Prices are ``Decimal`` (never
float), volume a non-negative integer, and OHLC invariants are enforced at construction. A series
carries a single :class:`PriceAdjustment` — RAW and ADJUSTED bars are never mixed. Nothing here
knows a provider, a strategy, or a readiness concept: universe membership, historical completeness,
and strategy readiness are strictly separate (completeness is Phase F; readiness is Phase G).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PriceAdjustment(StrEnum):
    """Whether a bar's prices are raw as-traded or corporate-action adjusted."""

    RAW = "raw"
    ADJUSTED = "adjusted"


class HistoryRequirement(BaseModel):
    """A generic, strategy-agnostic historical lookback requirement (trading days)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trading_days: int = Field(ge=1)


class HistoricalDailyBar(BaseModel):
    """One immutable canonical daily OHLCV bar for one instrument on one trading date."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    instrument_identity: str = Field(min_length=1, max_length=128)
    trading_date: date
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    adjustment: PriceAdjustment = PriceAdjustment.RAW
    source: str = Field(min_length=1, max_length=128)
    source_version: str | None = Field(default=None, min_length=1, max_length=64)
    ingested_at: datetime

    @model_validator(mode="after")
    def _validate_ohlc_and_awareness(self) -> HistoricalDailyBar:
        if self.high < self.low:
            raise ValueError("bar high must be >= low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("bar open must be within [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("bar close must be within [low, high]")
        if self.ingested_at.tzinfo is None or self.ingested_at.utcoffset() is None:
            raise ValueError("ingested_at must be timezone-aware")
        return self

    def has_same_values(self, other: HistoricalDailyBar) -> bool:
        """Whether the OHLCV + adjustment values match (ignores provenance, for idempotency)."""
        return (
            self.open == other.open
            and self.high == other.high
            and self.low == other.low
            and self.close == other.close
            and self.volume == other.volume
            and self.adjustment is other.adjustment
        )


class HistoricalSeries(BaseModel):
    """An immutable, date-sorted, gap-tolerant series of one instrument's daily bars."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instrument_identity: str = Field(min_length=1, max_length=128)
    adjustment: PriceAdjustment
    bars: tuple[HistoricalDailyBar, ...]

    @model_validator(mode="after")
    def _validate_ordering_identity_and_adjustment(self) -> HistoricalSeries:
        dates = [bar.trading_date for bar in self.bars]
        if dates != sorted(dates):
            raise ValueError("series bars must be sorted ascending by trading_date")
        if len(dates) != len(set(dates)):
            raise ValueError("series must not contain duplicate trading dates")
        for bar in self.bars:
            if bar.instrument_identity != self.instrument_identity:
                raise ValueError("all bars must share the series instrument identity")
            if bar.adjustment is not self.adjustment:
                raise ValueError("series must not mix raw and adjusted bars")
        return self

    @property
    def trading_dates(self) -> tuple[date, ...]:
        """Sorted trading dates present in the series."""
        return tuple(bar.trading_date for bar in self.bars)

    def latest(self) -> HistoricalDailyBar | None:
        """Return the most recent bar, or None if empty."""
        return self.bars[-1] if self.bars else None

    def previous_trading_bar(self, before: date) -> HistoricalDailyBar | None:
        """Return the latest stored bar strictly before ``before`` (None if none)."""
        earlier = [bar for bar in self.bars if bar.trading_date < before]
        return earlier[-1] if earlier else None

    def last_n(self, n: int) -> HistoricalSeries:
        """Return a series of the most recent ``n`` bars (fewer if unavailable)."""
        if n < 0:
            raise ValueError("n must be non-negative")
        return self.model_copy(update={"bars": self.bars[-n:] if n else ()})

    def between(self, start: date, end: date) -> HistoricalSeries:
        """Return a series of bars with ``start <= trading_date <= end`` (inclusive)."""
        selected = tuple(bar for bar in self.bars if start <= bar.trading_date <= end)
        return self.model_copy(update={"bars": selected})
