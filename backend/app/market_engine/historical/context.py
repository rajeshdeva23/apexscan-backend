"""Immutable historical-context value objects for the Market Engine (P4.5A).

These types carry *authoritative* historical facts only — canonical
:class:`~app.schemas.market_data.Candle` values — and never in-progress or
non-authoritative candle data (no ``PartialCandle``/``IncompleteCandle``), no
provider objects, and no loading/readiness status (that is P4.5B's concern).
A :class:`HistoricalContext` is one immutable snapshot for one instrument,
installed atomically and surfaced inside the MarketContext by later slices.

Ordering is normalised deterministically: candles within a series and series
within a context are sorted at construction, so equal inputs always produce
equal snapshots regardless of input order. Malformed identity (duplicate or
overlapping intervals, mixed instruments) is rejected rather than hidden.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, model_validator

from app.market_engine.historical.requirements import timeframe_ordering_key
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_HISTORICAL_MODEL_CONFIG = ConfigDict(
    arbitrary_types_allowed=True,
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
    strict=True,
)


class HistoricalSeries(BaseModel):
    """An immutable, chronologically ordered run of authoritative candles.

    All candles share one instrument and one timeframe. Intervals are unique and
    non-overlapping. Input order is normalised to ascending start time; an empty
    series is rejected — absence of history is represented by the absence of the
    timeframe's series in the enclosing context, not by an empty series.

    Attributes:
        timeframe: The timeframe every candle in the series belongs to.
        candles: The authoritative candles, stored oldest-first.
    """

    model_config = _HISTORICAL_MODEL_CONFIG

    timeframe: Timeframe
    candles: tuple[Candle, ...]

    @model_validator(mode="before")
    @classmethod
    def _order_candles(cls, data: object) -> object:
        """Normalise candle input to ascending (start, end) order before validation."""
        if isinstance(data, dict):
            candles = data.get("candles")
            if isinstance(candles, list | tuple) and all(isinstance(c, Candle) for c in candles):
                ordered = tuple(sorted(candles, key=lambda c: (c.start_timestamp, c.end_timestamp)))
                return {**data, "candles": ordered}
        return data

    @model_validator(mode="after")
    def _validate_identity(self) -> HistoricalSeries:
        if not self.candles:
            raise ValueError("a historical series must contain at least one candle")
        if len({candle.instrument for candle in self.candles}) != 1:
            raise ValueError("all candles in a series must share one instrument")
        for previous, current in zip(self.candles, self.candles[1:], strict=False):
            same_interval = (
                current.start_timestamp == previous.start_timestamp
                and current.end_timestamp == previous.end_timestamp
            )
            if same_interval:
                raise ValueError("a historical series must not contain duplicate intervals")
            if current.start_timestamp < previous.end_timestamp:
                raise ValueError("a historical series must not contain overlapping intervals")
        return self

    @property
    def instrument(self) -> Instrument:
        """Return the single instrument shared by every candle in the series."""
        return self.candles[0].instrument


class PreviousSessionFacts(BaseModel):
    """The previous trading session's authoritative daily candle, as a fact.

    Stores the authoritative :class:`Candle` rather than copied OHLC values, so
    consumers derive previous open/high/low/close/volume from one source of truth.
    The ``trading_date`` is supplied authoritatively and is not re-derived from the
    candle: the canonical candle carries no timezone or trading date, and deriving
    one from a host timezone would be incorrect (ADR-006 §10; docs/06 §8).

    Attributes:
        trading_date: The exchange-local date of the previous trading session.
        candle: The authoritative candle for that session.
    """

    model_config = _HISTORICAL_MODEL_CONFIG

    trading_date: date
    candle: Candle

    @property
    def instrument(self) -> Instrument:
        """Return the instrument the previous-session candle belongs to."""
        return self.candle.instrument


class HistoricalContext(BaseModel):
    """One immutable historical snapshot for a single instrument.

    Holds at most one :class:`HistoricalSeries` per timeframe plus optional
    previous-session facts, all belonging to the context instrument. Series are
    ordered deterministically. A context with ``previous_session=None`` and no
    series is valid and means "explicitly installed but carrying no authoritative
    historical facts" — distinct from ``MarketContext.historical`` being ``None``,
    which means no snapshot has been installed at all.

    Attributes:
        instrument: The instrument this snapshot describes.
        previous_session: Optional previous-session facts for the instrument.
        series: The per-timeframe historical series, ordered deterministically.
    """

    model_config = _HISTORICAL_MODEL_CONFIG

    instrument: Instrument
    previous_session: PreviousSessionFacts | None = None
    series: tuple[HistoricalSeries, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _order_series(cls, data: object) -> object:
        """Normalise series input to deterministic timeframe order before validation."""
        if isinstance(data, dict):
            series = data.get("series")
            if isinstance(series, list | tuple) and all(
                isinstance(item, HistoricalSeries) for item in series
            ):
                ordered = tuple(
                    sorted(series, key=lambda item: timeframe_ordering_key(item.timeframe))
                )
                return {**data, "series": ordered}
        return data

    @model_validator(mode="after")
    def _validate_membership(self) -> HistoricalContext:
        timeframes = [item.timeframe for item in self.series]
        if len(set(timeframes)) != len(timeframes):
            raise ValueError("a historical context may hold at most one series per timeframe")
        for item in self.series:
            if item.instrument != self.instrument:
                raise ValueError("every series must belong to the context instrument")
        previous = self.previous_session
        if previous is not None and previous.instrument != self.instrument:
            raise ValueError("previous-session facts must belong to the context instrument")
        return self
