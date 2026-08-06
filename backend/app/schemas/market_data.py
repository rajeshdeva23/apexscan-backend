"""Broker-neutral market-data contracts for the Data Provider boundary.

These immutable models express data after a provider adapter has normalized it.
``event_timestamp`` is always the exchange/event time supplied by the provider;
it is deliberately distinct from any future ingestion or processing timestamp.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _CanonicalModel(BaseModel):
    """Shared strict, immutable configuration for canonical market-data values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        str_strip_whitespace=True,
    )


class MarketSegment(StrEnum):
    """Canonical market segments independent of a provider's wire-level codes."""

    EQUITY = "equity"
    DERIVATIVES = "derivatives"
    CURRENCY = "currency"
    COMMODITY = "commodity"
    INDEX = "index"


class InstrumentClass(StrEnum):
    """Canonical broad classes used for tradable-instrument validation."""

    CASH = "cash"
    FUTURE = "future"
    OPTION = "option"
    OTHER = "other"


class OptionType(StrEnum):
    """Canonical option sides."""

    CALL = "call"
    PUT = "put"


class UnderlyingInstrument(_CanonicalModel):
    """Provider-neutral economic underlying identity, separate from a contract."""

    exchange: str = Field(min_length=1)
    symbol: str = Field(min_length=1)

    @field_validator("exchange", "symbol")
    @classmethod
    def _normalize_identity_component(cls, value: str) -> str:
        """Store a deterministic exchange/symbol identity without provider IDs."""
        normalized_value = value.upper()
        if not normalized_value:
            raise ValueError("instrument identity components must not be empty")
        return normalized_value


class Instrument(_CanonicalModel):
    """Provider-neutral identity for one tradable listing or derivative contract."""

    exchange: str = Field(min_length=1)
    market_segment: MarketSegment = MarketSegment.EQUITY
    symbol: str = Field(min_length=1)
    instrument_class: InstrumentClass = InstrumentClass.CASH
    underlying: UnderlyingInstrument | None = None
    display_name: str | None = Field(default=None, min_length=1)
    listing_type: str | None = Field(default=None, min_length=1)
    series: str | None = Field(default=None, min_length=1)
    expiry: date | None = None
    strike_price: Decimal | None = Field(default=None, gt=0)
    option_type: OptionType | None = None

    @field_validator("exchange", "symbol")
    @classmethod
    def _normalize_identity_component(cls, value: str) -> str:
        """Store deterministic exchange/listing identity without provider IDs."""
        normalized_value = value.upper()
        if not normalized_value:
            raise ValueError("instrument identity components must not be empty")
        return normalized_value

    @field_validator("display_name", "listing_type", "series")
    @classmethod
    def _normalize_optional_contract_component(cls, value: str | None) -> str | None:
        """Store optional exchange-defined classifications deterministically."""
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def _validate_contract_identity(self) -> Instrument:
        """Require each derivative's provider-neutral distinguishing fields."""
        if self.instrument_class is InstrumentClass.FUTURE:
            if self.underlying is None or self.expiry is None:
                raise ValueError("future contracts require an underlying and expiry")
            if self.strike_price is not None or self.option_type is not None:
                raise ValueError("future contracts must not define option identity fields")
        elif self.instrument_class is InstrumentClass.OPTION:
            if (
                self.underlying is None
                or self.expiry is None
                or self.strike_price is None
                or self.option_type is None
            ):
                raise ValueError(
                    "option contracts require an underlying, expiry, strike price, and option type"
                )
        elif any(
            value is not None
            for value in (self.underlying, self.expiry, self.strike_price, self.option_type)
        ):
            raise ValueError(
                "non-derivative instruments must not define derivative identity fields"
            )
        return self


class MarketDataKind(StrEnum):
    """Canonical live market-data kinds a consumer may request."""

    TICK = "tick"
    QUOTE = "quote"
    DEPTH = "depth"
    CANDLE = "candle"


class ProviderStatus(StrEnum):
    """Provider health states reported by a broker adapter."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


class ProviderCapability(StrEnum):
    """Independently supported broker-adapter capabilities."""

    LIVE_MARKET_DATA = "live_market_data"
    HISTORICAL_DATA = "historical_data"
    INSTRUMENTS = "instruments"


def _require_aware_timestamp(value: datetime) -> datetime:
    """Reject ambiguous clocks and normalize accepted timestamps to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class _EventData(_CanonicalModel):
    """Common fields for a provider event observed at the exchange."""

    instrument: Instrument
    event_timestamp: datetime

    _validate_event_timestamp = field_validator("event_timestamp")(_require_aware_timestamp)


class Tick(_EventData):
    """One event-time last-traded price and optional traded quantity in units."""

    last_price: Decimal = Field(gt=0)
    traded_quantity: int | None = Field(default=None, ge=0)


class Quote(_EventData):
    """One event-time best bid/ask quote; quantities are instrument units."""

    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    bid_quantity: int = Field(ge=0)
    ask_quantity: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_price_order(self) -> Quote:
        if self.ask_price < self.bid_price:
            raise ValueError("ask price must be greater than or equal to bid price")
        return self


class DepthLevel(_CanonicalModel):
    """One price level with quantity expressed in instrument units."""

    price: Decimal = Field(gt=0)
    quantity: int = Field(ge=0)


class DepthSnapshot(_EventData):
    """An event-time order-book snapshot with canonical bid and ask levels."""

    bids: tuple[DepthLevel, ...] = Field(min_length=1)
    asks: tuple[DepthLevel, ...] = Field(min_length=1)


class Candle(_CanonicalModel):
    """OHLCV data over a closed event-time interval; quantity is traded units."""

    instrument: Instrument
    start_timestamp: datetime
    end_timestamp: datetime
    open_price: Decimal = Field(gt=0)
    high_price: Decimal = Field(gt=0)
    low_price: Decimal = Field(gt=0)
    close_price: Decimal = Field(gt=0)
    traded_quantity: int = Field(ge=0)

    _validate_start_timestamp = field_validator("start_timestamp")(_require_aware_timestamp)
    _validate_end_timestamp = field_validator("end_timestamp")(_require_aware_timestamp)

    @model_validator(mode="after")
    def _validate_candle_interval_and_prices(self) -> Candle:
        if self.end_timestamp <= self.start_timestamp:
            raise ValueError("candle end timestamp must be after start timestamp")
        if self.high_price < self.low_price:
            raise ValueError("candle high price must be greater than or equal to low price")
        if not self.low_price <= self.open_price <= self.high_price:
            raise ValueError("candle open price must be within the high-low range")
        if not self.low_price <= self.close_price <= self.high_price:
            raise ValueError("candle close price must be within the high-low range")
        return self


class HistoricalRequest(_CanonicalModel):
    """A request for canonical candles over an explicit time interval."""

    instrument: Instrument
    start_timestamp: datetime
    end_timestamp: datetime
    interval: timedelta

    _validate_start_timestamp = field_validator("start_timestamp")(_require_aware_timestamp)
    _validate_end_timestamp = field_validator("end_timestamp")(_require_aware_timestamp)

    @model_validator(mode="after")
    def _validate_range_and_interval(self) -> HistoricalRequest:
        if self.end_timestamp <= self.start_timestamp:
            raise ValueError("historical end timestamp must be after start timestamp")
        if self.interval <= timedelta(0):
            raise ValueError("historical interval must be positive")
        return self


class HistoricalResult(_CanonicalModel):
    """Canonical historical candle result matching one broker-neutral request."""

    request: HistoricalRequest
    candles: tuple[Candle, ...]

    @model_validator(mode="after")
    def _validate_candle_instruments(self) -> HistoricalResult:
        if any(candle.instrument != self.request.instrument for candle in self.candles):
            raise ValueError("historical candles must match the requested instrument")
        return self


class SubscriptionRequest(_CanonicalModel):
    """A consumer's canonical live-data subscription intent."""

    instruments: tuple[Instrument, ...] = Field(min_length=1)
    data_types: frozenset[MarketDataKind] = Field(min_length=1)

    @field_validator("instruments")
    @classmethod
    def _require_unique_instruments(cls, value: tuple[Instrument, ...]) -> tuple[Instrument, ...]:
        if len(value) != len(set(value)):
            raise ValueError("subscription instruments must be unique")
        return value


class ProviderHealth(_CanonicalModel):
    """A point-in-time broker health observation, not application readiness."""

    status: ProviderStatus
    observed_at: datetime

    _validate_observed_at = field_validator("observed_at")(_require_aware_timestamp)


MarketData = Tick | Quote | DepthSnapshot | Candle
