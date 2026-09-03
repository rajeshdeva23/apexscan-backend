"""The immutable, versioned MarketContext — the Market Engine's core product.

MarketContext is a complete, self-contained, versioned snapshot of an
instrument's market state at a point in time (docs/06 §6). It is immutable: an
update never mutates an existing snapshot, it produces a new version instead
(docs/06 §6.5, §28.4). This module defines only the snapshot container and its
versioning discipline; it computes no ticks, candles, sessions, or features —
those data slots are populated by later Market Engine slices and the contract
evolves additively (docs/06 §21.6, §29).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.market_engine.historical.context import HistoricalContext
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument, Quote, Tick

_INITIAL_VERSION = 1


class MarketState(StrEnum):
    """The explicit, authoritative market phase stamped into a MarketContext (docs/06 §7).

    These are session phases as facts, never trading judgements. "Market open"
    and the regular continuous session (docs/06 §7.1) are represented by
    ``LIVE_SESSION``; the momentary open is that phase's inclusive start boundary.

    ``CALENDAR_UNAVAILABLE`` means the instant's exchange-local trading date lies
    outside the authoritative :class:`CalendarCoverage`, so the trading status is
    not known (ADR-011 live out-of-coverage addendum LC2/LC8). It is treated
    fail-closed and is mutually exclusive with every phase/closed state: it is
    **not** ``LIVE_SESSION``, ``HOLIDAY``, ``MARKET_CLOSED``, or ``EMERGENCY_HALT``,
    and is never inferred as trading, closed, or holiday.
    """

    PRE_OPEN = "pre_open"
    OPENING_AUCTION = "opening_auction"
    LIVE_SESSION = "live_session"
    CLOSING_SESSION = "closing_session"
    MARKET_CLOSED = "market_closed"
    HOLIDAY = "holiday"
    EMERGENCY_HALT = "emergency_halt"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"


def _require_utc(value: datetime) -> datetime:
    """Reject naive timestamps and normalise accepted ones to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class _FrozenModel(BaseModel):
    """Shared strict, immutable configuration for Market Engine value objects."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        str_strip_whitespace=True,
    )


class CandleQuality(StrEnum):
    """Broker-neutral completeness of a candle interval (ADR-006 §8).

    Only ``COMPLETE`` intervals may become a canonical :class:`Candle`. The other
    states describe why an interval is not yet authoritative; any finalized
    non-``COMPLETE`` interval is retained as an :class:`IncompleteCandle` and is
    thereby awaiting P4.5 reconciliation (ADR-006's "awaiting backfill" is that
    structural state rather than a separate enum value).
    """

    COMPLETE = "complete"
    INCOMPLETE_OHLC = "incomplete_ohlc"
    INCOMPLETE_VOLUME = "incomplete_volume"
    FEED_GAP = "feed_gap"


class MarketFact(_FrozenModel):
    """One standardized, named market fact carried by a MarketContext.

    A fact is a neutral measurement, never a trading signal or decision
    (docs/13 §5). The value union is deliberately generic; no trading semantics
    are implied here.
    """

    name: str = Field(min_length=1)
    value: Decimal | int | str | bool


class SessionContext(_FrozenModel):
    """Broker-neutral session facts carried by a MarketContext (docs/06 §7-§8).

    Attributes:
        trading_date: The exchange-local trading date the snapshot belongs to.
        market_state: The explicit market phase at the snapshot's event time.
        exchange_timezone: The IANA timezone used to interpret the session
            (e.g. "Asia/Kolkata"); canonical timestamps remain UTC.
    """

    trading_date: date
    market_state: MarketState
    exchange_timezone: str = Field(min_length=1)


class SessionStatisticsQuality(StrEnum):
    """Authority of a current-session statistics fact (ADR-008 D6).

    The initial governed model has exactly two states. ``AUTHORITATIVE`` means the
    values come from a verified provider session aggregate (ADR-008 D3/D4);
    ``UNAVAILABLE`` means no verified aggregate is available or the session phase does
    not permit regular-session statistics. No feed-gap/incomplete/stale sub-states are
    introduced.
    """

    AUTHORITATIVE = "authoritative"
    UNAVAILABLE = "unavailable"


class SessionStatistics(_FrozenModel):
    """Immutable current-session statistics fact for one instrument (docs/06 §17; ADR-008).

    A broker-neutral market fact — never a signal, score, rank, or strategy verdict.
    An ``AUTHORITATIVE`` value carries a coherent regular-session open/high/low from a
    single verified provider aggregate; an ``UNAVAILABLE`` value carries no prices. The
    absence of any current statistics is represented by ``None`` at the owning state,
    not by a stale-priced object.

    Attributes:
        trading_date: The exchange-local trading date the statistics belong to.
        open_price: The authoritative regular-session open, or ``None`` when unavailable.
        high_price: The authoritative session-to-date high, or ``None`` when unavailable.
        low_price: The authoritative session-to-date low, or ``None`` when unavailable.
        quality: The authority state of these statistics.
        as_of: The event time of the aggregate the statistics were built from (UTC).
    """

    trading_date: date
    open_price: Decimal | None = Field(default=None, gt=0)
    high_price: Decimal | None = Field(default=None, gt=0)
    low_price: Decimal | None = Field(default=None, gt=0)
    quality: SessionStatisticsQuality
    as_of: datetime

    _validate_as_of = field_validator("as_of")(_require_utc)

    @model_validator(mode="after")
    def _validate_quality_consistency(self) -> SessionStatistics:
        open_price, high_price, low_price = self.open_price, self.high_price, self.low_price
        if self.quality is SessionStatisticsQuality.UNAVAILABLE:
            if any(price is not None for price in (open_price, high_price, low_price)):
                raise ValueError("unavailable session statistics must carry no prices")
            return self
        if open_price is None or high_price is None or low_price is None:
            raise ValueError("authoritative session statistics require open, high, and low prices")
        if high_price < low_price:
            raise ValueError("session high price must be greater than or equal to low price")
        if not low_price <= open_price <= high_price:
            raise ValueError("session open price must be within the high-low range")
        return self


class PartialCandle(_FrozenModel):
    """An immutable in-progress candle: OHLC(V) facts for a not-yet-closed interval.

    Distinct from the canonical :class:`~app.schemas.market_data.Candle` so an
    in-progress interval is never mistaken for a finalized one (docs/06 §13.2).
    ``traded_quantity`` is the interval volume so far, or ``None`` when no
    authoritative cumulative-volume baseline is available yet (ADR-005).
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    start_timestamp: datetime
    end_timestamp: datetime
    open_price: Decimal = Field(gt=0)
    high_price: Decimal = Field(gt=0)
    low_price: Decimal = Field(gt=0)
    close_price: Decimal = Field(gt=0)
    traded_quantity: int | None = Field(default=None, ge=0)
    quality: CandleQuality = CandleQuality.INCOMPLETE_VOLUME

    _validate_start = field_validator("start_timestamp")(_require_utc)
    _validate_end = field_validator("end_timestamp")(_require_utc)


class IncompleteCandle(_FrozenModel):
    """A finalized interval whose OHLCV is not yet authoritative (ADR-006 §2, §8).

    Retained (never emitted as a canonical :class:`Candle`) so P4.5 reconciliation
    has something to repair. Carries the provisionally observed OHLC and, where a
    baseline exists, a provisional volume — all non-authoritative, flagged by
    ``quality``. ``quality`` is never ``COMPLETE`` here.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    instrument: Instrument
    timeframe: Timeframe
    start_timestamp: datetime
    end_timestamp: datetime
    open_price: Decimal = Field(gt=0)
    high_price: Decimal = Field(gt=0)
    low_price: Decimal = Field(gt=0)
    close_price: Decimal = Field(gt=0)
    traded_quantity: int | None = Field(default=None, ge=0)
    quality: CandleQuality

    _validate_start = field_validator("start_timestamp")(_require_utc)
    _validate_end = field_validator("end_timestamp")(_require_utc)


class TimeframeCandles(_FrozenModel):
    """The immutable candle state for one instrument at one timeframe (docs/06 §6.3).

    Attributes:
        timeframe: The timeframe these candles are aggregated at.
        partial: The current in-progress candle, if any.
        finalized: A bounded, chronological tuple of *authoritative* finalized candles.
        incomplete: A bounded, chronological tuple of finalized-but-non-authoritative
            intervals awaiting P4.5 reconciliation (never mixed with ``finalized``).
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    timeframe: Timeframe
    partial: PartialCandle | None = None
    finalized: tuple[Candle, ...] = ()
    incomplete: tuple[IncompleteCandle, ...] = ()


class MarketContext(_FrozenModel):
    """An immutable, versioned snapshot of one instrument's market state.

    Every field is read-only after construction. A new observation is expressed
    as a new version via :meth:`with_update`; the object itself is never mutated.
    """

    instrument: Instrument
    version: int = Field(ge=_INITIAL_VERSION)
    sequence: int = Field(ge=0)
    event_timestamp: datetime
    observed_at: datetime
    latest_tick: Tick | None = None
    latest_quote: Quote | None = None
    latest_candle: Candle | None = None
    candle_sets: tuple[TimeframeCandles, ...] = ()
    session: SessionContext | None = None
    facts: tuple[MarketFact, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    historical: HistoricalContext | None = None
    session_statistics: SessionStatistics | None = None
    previous_close: Decimal | None = Field(default=None, gt=0)
    is_valid: bool = True

    _validate_event_timestamp = field_validator("event_timestamp")(_require_utc)
    _validate_observed_at = field_validator("observed_at")(_require_utc)

    @classmethod
    def initial(
        cls,
        instrument: Instrument,
        *,
        sequence: int,
        event_timestamp: datetime,
        observed_at: datetime,
        latest_tick: Tick | None = None,
        latest_quote: Quote | None = None,
        latest_candle: Candle | None = None,
        candle_sets: tuple[TimeframeCandles, ...] = (),
        session: SessionContext | None = None,
        facts: tuple[MarketFact, ...] = (),
        metadata: tuple[tuple[str, str], ...] = (),
        historical: HistoricalContext | None = None,
        session_statistics: SessionStatistics | None = None,
        previous_close: Decimal | None = None,
        is_valid: bool = True,
    ) -> MarketContext:
        """Build the first (version 1) snapshot for an instrument.

        Args:
            instrument: The canonical instrument this snapshot describes.
            sequence: The deterministic ordinal from the sequence generator.
            event_timestamp: The exchange/event time the snapshot represents.
            observed_at: The build instant, taken from the injected clock.
            latest_tick: Optional current tick state.
            latest_quote: Optional current quote state.
            latest_candle: Optional current candle reference.
            candle_sets: Per-timeframe candle facts (partial + finalized).
            session: Optional session facts (trading date, market phase, timezone).
            facts: Optional standardized market facts.
            metadata: Optional immutable provenance key/value pairs.
            historical: Optional immutable historical-context snapshot.
            session_statistics: Optional current-session statistics fact (ADR-008).
            previous_close: Optional prior-session reference close for this instrument.
            is_valid: Whether the snapshot is complete and trustworthy.

        Returns:
            A fresh, immutable MarketContext at version 1.
        """
        return cls(
            instrument=instrument,
            version=_INITIAL_VERSION,
            sequence=sequence,
            event_timestamp=event_timestamp,
            observed_at=observed_at,
            latest_tick=latest_tick,
            latest_quote=latest_quote,
            latest_candle=latest_candle,
            candle_sets=candle_sets,
            session=session,
            facts=facts,
            metadata=metadata,
            historical=historical,
            session_statistics=session_statistics,
            previous_close=previous_close,
            is_valid=is_valid,
        )

    def with_update(
        self,
        *,
        sequence: int,
        event_timestamp: datetime,
        observed_at: datetime,
        latest_tick: Tick | None = None,
        latest_quote: Quote | None = None,
        latest_candle: Candle | None = None,
        candle_sets: tuple[TimeframeCandles, ...] = (),
        session: SessionContext | None = None,
        facts: tuple[MarketFact, ...] = (),
        metadata: tuple[tuple[str, str], ...] = (),
        historical: HistoricalContext | None = None,
        session_statistics: SessionStatistics | None = None,
        previous_close: Decimal | None = None,
        is_valid: bool = True,
    ) -> MarketContext:
        """Return a new snapshot for the same instrument at ``version + 1``.

        This method never mutates ``self``; it constructs and returns a new
        immutable MarketContext whose version is exactly one greater. The caller
        supplies the observable state for the new snapshot (the merge policy for
        carrying data forward belongs to later engine slices).

        Args:
            sequence: The deterministic ordinal for the new snapshot.
            event_timestamp: The exchange/event time the new snapshot represents.
            observed_at: The build instant, taken from the injected clock.
            latest_tick: Current tick state for the new snapshot.
            latest_quote: Current quote state for the new snapshot.
            latest_candle: Current candle reference for the new snapshot.
            candle_sets: Per-timeframe candle facts (partial + finalized).
            session: Session facts (trading date, market phase, timezone).
            facts: Standardized market facts for the new snapshot.
            metadata: Immutable provenance key/value pairs.
            historical: Immutable historical-context snapshot carried forward.
            session_statistics: Current-session statistics fact carried forward (ADR-008).
            previous_close: Prior-session reference close for this instrument; replaces the
                prior value (``None`` clears it — the engine carries it forward explicitly).
            is_valid: Whether the new snapshot is complete and trustworthy.

        Returns:
            A new MarketContext at ``self.version + 1``.
        """
        return MarketContext(
            instrument=self.instrument,
            version=self.version + 1,
            sequence=sequence,
            event_timestamp=event_timestamp,
            observed_at=observed_at,
            latest_tick=latest_tick,
            latest_quote=latest_quote,
            latest_candle=latest_candle,
            candle_sets=candle_sets,
            session=session,
            facts=facts,
            metadata=metadata,
            historical=historical,
            session_statistics=session_statistics,
            previous_close=previous_close,
            is_valid=is_valid,
        )
