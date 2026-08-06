"""Dhan-specific normalization into the P3.1 canonical provider contracts."""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

from pydantic import ValidationError

from app.adapters.base.errors import NormalizationError
from app.adapters.dhan.models import (
    DhanCashEquityLiveUniverse,
    DhanFnoStockUniverse,
    DhanInstrumentReference,
)
from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    InstrumentClass,
    MarketSegment,
    OptionType,
    UnderlyingInstrument,
)

_REQUIRED_INSTRUMENT_COLUMNS = frozenset(
    {
        "EXCH_ID",
        "SEGMENT",
        "SECURITY_ID",
        "INSTRUMENT",
        "UNDERLYING_SYMBOL",
        "SYMBOL_NAME",
        "DISPLAY_NAME",
        "INSTRUMENT_TYPE",
        "SERIES",
        "SM_EXPIRY_DATE",
        "STRIKE_PRICE",
        "OPTION_TYPE",
    }
)
_EXCHANGE_SEGMENTS = {
    ("NSE", "E"): "NSE_EQ",
    ("NSE", "D"): "NSE_FNO",
    ("NSE", "C"): "NSE_CURRENCY",
    ("BSE", "E"): "BSE_EQ",
    ("BSE", "D"): "BSE_FNO",
    ("BSE", "C"): "BSE_CURRENCY",
    ("MCX", "M"): "MCX_COMM",
    ("IDX", "I"): "IDX_I",
}
_MARKET_SEGMENTS = {
    "E": MarketSegment.EQUITY,
    "D": MarketSegment.DERIVATIVES,
    "C": MarketSegment.CURRENCY,
    "M": MarketSegment.COMMODITY,
    "I": MarketSegment.INDEX,
}
_HISTORICAL_FIELDS = ("open", "high", "low", "close", "volume", "timestamp")


def normalize_instrument_master(csv_text: str) -> tuple[DhanInstrumentReference, ...]:
    """Normalize documented detailed-master CSV rows without leaking security IDs upward."""
    try:
        reader = csv.DictReader(StringIO(csv_text))
        field_names = frozenset(reader.fieldnames or ())
        if not _REQUIRED_INSTRUMENT_COLUMNS.issubset(field_names):
            raise NormalizationError()

        references_by_instrument: dict[Instrument, DhanInstrumentReference] = {}
        for row in reader:
            reference = _normalize_instrument_row(row)
            if reference.instrument in references_by_instrument:
                raise NormalizationError()
            references_by_instrument[reference.instrument] = reference
    except (csv.Error, TypeError, ValueError) as error:
        raise NormalizationError() from error

    return tuple(references_by_instrument.values())


def derive_equity_fno_universe(
    references: tuple[DhanInstrumentReference, ...],
) -> DhanFnoStockUniverse:
    """Derive production NSE stock derivatives through linked exchange cash metadata."""
    cash_equities_by_security_id = {
        reference.security_id: reference
        for reference in references
        if _is_nse_equity_share(reference)
    }
    stock_derivatives = tuple(
        reference
        for reference in references
        if _is_production_nse_stock_derivative(reference, cash_equities_by_security_id)
    )
    futures = tuple(
        sorted(
            (
                reference
                for reference in stock_derivatives
                if reference.provider_instrument_type == "FUTSTK"
            ),
            key=_reference_sort_key,
        )
    )
    options = tuple(
        sorted(
            (
                reference
                for reference in stock_derivatives
                if reference.provider_instrument_type == "OPTSTK"
            ),
            key=_reference_sort_key,
        )
    )
    underlyings = tuple(
        sorted(
            {
                reference.instrument.underlying
                for reference in (*futures, *options)
                if reference.instrument.underlying is not None
            },
            key=lambda underlying: (underlying.exchange, underlying.symbol),
        )
    )
    return DhanFnoStockUniverse(futures=futures, options=options, underlyings=underlyings)


def resolve_nse_cash_equity_live_universe(
    references: tuple[DhanInstrumentReference, ...],
) -> DhanCashEquityLiveUniverse:
    """Resolve the cash-equity provider references for validated F&O underlyings."""
    references_by_security_id = {reference.security_id: reference for reference in references}
    cash_equities_by_security_id = {
        reference.security_id: reference
        for reference in references
        if _is_nse_equity_share(reference)
    }
    references_by_underlying: dict[UnderlyingInstrument, dict[str, DhanInstrumentReference]] = {}
    missing_underlyings: set[UnderlyingInstrument] = set()
    symbol_mismatches: set[UnderlyingInstrument] = set()

    for derivative in references:
        if not _is_nse_stock_derivative_candidate(derivative):
            continue
        underlying = derivative.instrument.underlying
        if underlying is None or derivative.underlying_security_id is None:
            raise NormalizationError()
        linked_reference = references_by_security_id.get(derivative.underlying_security_id)
        if linked_reference is None:
            missing_underlyings.add(underlying)
            continue
        cash_reference = cash_equities_by_security_id.get(derivative.underlying_security_id)
        if cash_reference is None:
            continue
        if cash_reference.instrument.symbol != underlying.symbol:
            symbol_mismatches.add(underlying)
            continue
        references_by_underlying.setdefault(underlying, {})[cash_reference.security_id] = (
            cash_reference
        )

    ambiguous_underlyings = {
        underlying
        for underlying, cash_references in references_by_underlying.items()
        if len(cash_references) != 1
    }
    blocked_underlyings = missing_underlyings | symbol_mismatches | ambiguous_underlyings
    resolved_underlyings = tuple(
        sorted(
            (
                underlying
                for underlying, cash_references in references_by_underlying.items()
                if underlying not in blocked_underlyings and len(cash_references) == 1
            ),
            key=lambda underlying: (underlying.exchange, underlying.symbol),
        )
    )

    return DhanCashEquityLiveUniverse(
        underlyings=resolved_underlyings,
        cash_references=tuple(
            next(iter(references_by_underlying[underlying].values()))
            for underlying in resolved_underlyings
        ),
        missing_underlyings=tuple(
            sorted(
                missing_underlyings, key=lambda underlying: (underlying.exchange, underlying.symbol)
            )
        ),
        ambiguous_underlyings=tuple(
            sorted(
                ambiguous_underlyings,
                key=lambda underlying: (underlying.exchange, underlying.symbol),
            )
        ),
        symbol_mismatches=tuple(
            sorted(
                symbol_mismatches, key=lambda underlying: (underlying.exchange, underlying.symbol)
            )
        ),
    )


def normalize_historical_payload(
    request: HistoricalRequest, payload: Mapping[str, object]
) -> HistoricalResult:
    """Convert Dhan parallel OHLCV arrays and epoch seconds into canonical candles."""
    arrays = _historical_arrays(payload)
    candles: list[Candle] = []
    try:
        for values in zip(*(arrays[field] for field in _HISTORICAL_FIELDS), strict=True):
            open_price, high_price, low_price, close_price, volume, timestamp = values
            start_timestamp = _epoch_timestamp(timestamp)
            candles.append(
                Candle(
                    instrument=request.instrument,
                    start_timestamp=start_timestamp,
                    end_timestamp=start_timestamp + request.interval,
                    open_price=_decimal(open_price),
                    high_price=_decimal(high_price),
                    low_price=_decimal(low_price),
                    close_price=_decimal(close_price),
                    traded_quantity=_volume(volume),
                )
            )
        return HistoricalResult(request=request, candles=tuple(candles))
    except (InvalidOperation, OverflowError, TypeError, ValidationError, ValueError) as error:
        raise NormalizationError() from error


def _normalize_instrument_row(row: Mapping[str, str | None]) -> DhanInstrumentReference:
    exchange = _required_text(row, "EXCH_ID").upper()
    segment = _required_text(row, "SEGMENT").upper()
    exchange_segment = _EXCHANGE_SEGMENTS.get((exchange, segment))
    market_segment = _MARKET_SEGMENTS.get(segment)
    if market_segment is None:
        raise NormalizationError()

    provider_instrument_type = _required_text(row, "INSTRUMENT").upper()
    instrument_class = _instrument_class(provider_instrument_type)
    underlying_symbol = _optional_text(row, "UNDERLYING_SYMBOL")
    underlying = (
        UnderlyingInstrument(exchange=exchange, symbol=underlying_symbol)
        if instrument_class in {InstrumentClass.FUTURE, InstrumentClass.OPTION}
        and underlying_symbol is not None
        else None
    )
    return DhanInstrumentReference(
        instrument=Instrument(
            exchange=exchange,
            market_segment=market_segment,
            symbol=_canonical_listing_symbol(row, instrument_class),
            instrument_class=instrument_class,
            underlying=underlying,
            display_name=_optional_text(row, "DISPLAY_NAME"),
            listing_type=_optional_text(row, "INSTRUMENT_TYPE"),
            series=_optional_text(row, "SERIES"),
            expiry=_derivative_expiry(row, instrument_class),
            strike_price=_option_strike(row, instrument_class),
            option_type=_option_type(row, instrument_class),
        ),
        security_id=_required_text(row, "SECURITY_ID"),
        underlying_security_id=(
            _required_text(row, "UNDERLYING_SECURITY_ID")
            if instrument_class in {InstrumentClass.FUTURE, InstrumentClass.OPTION}
            else None
        ),
        exchange_segment=exchange_segment,
        provider_instrument_type=provider_instrument_type,
    )


def _canonical_listing_symbol(
    row: Mapping[str, str | None], instrument_class: InstrumentClass
) -> str:
    if instrument_class is InstrumentClass.CASH:
        cash_listing_symbol = _optional_text(row, "UNDERLYING_SYMBOL")
        if cash_listing_symbol is not None:
            return cash_listing_symbol
    return _required_text(row, "SYMBOL_NAME")


def _instrument_class(provider_instrument_type: str) -> InstrumentClass:
    if provider_instrument_type.startswith("FUT"):
        return InstrumentClass.FUTURE
    if provider_instrument_type.startswith("OPT"):
        return InstrumentClass.OPTION
    if provider_instrument_type == "EQUITY":
        return InstrumentClass.CASH
    return InstrumentClass.OTHER


def _derivative_expiry(
    row: Mapping[str, str | None], instrument_class: InstrumentClass
) -> date | None:
    if instrument_class not in {InstrumentClass.FUTURE, InstrumentClass.OPTION}:
        return None
    try:
        return date.fromisoformat(_required_text(row, "SM_EXPIRY_DATE"))
    except ValueError as error:
        raise NormalizationError() from error


def _option_strike(
    row: Mapping[str, str | None], instrument_class: InstrumentClass
) -> Decimal | None:
    if instrument_class is not InstrumentClass.OPTION:
        return None
    try:
        value = Decimal(_required_text(row, "STRIKE_PRICE"))
    except InvalidOperation as error:
        raise NormalizationError() from error
    if value <= 0:
        raise NormalizationError()
    return value


def _option_type(
    row: Mapping[str, str | None], instrument_class: InstrumentClass
) -> OptionType | None:
    if instrument_class is not InstrumentClass.OPTION:
        return None
    option_type = _required_text(row, "OPTION_TYPE").upper()
    if option_type == "CE":
        return OptionType.CALL
    if option_type == "PE":
        return OptionType.PUT
    raise NormalizationError()


def _historical_arrays(payload: Mapping[str, object]) -> dict[str, list[object]]:
    arrays: dict[str, list[object]] = {}
    try:
        for field in _HISTORICAL_FIELDS:
            value = payload[field]
            if not isinstance(value, list):
                raise NormalizationError()
            arrays[field] = value
    except (KeyError, TypeError) as error:
        raise NormalizationError() from error

    if len({len(values) for values in arrays.values()}) != 1:
        raise NormalizationError()
    return arrays


def _required_text(row: Mapping[str, str | None], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError()
    return value.strip()


def _optional_text(row: Mapping[str, str | None], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized if normalized and normalized not in {"NA", "N/A"} else None


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise NormalizationError()
    return Decimal(str(value))


def _volume(value: object) -> int:
    volume = _integral_number(value)
    if volume < 0:
        raise NormalizationError()
    return volume


def _epoch_timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(_integral_number(value), tz=UTC)


def _integral_number(value: object) -> int:
    """Normalize a Dhan JSON number only when it exactly represents an integer."""
    if isinstance(value, bool):
        raise NormalizationError()

    if isinstance(value, int):
        return value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)

    raise NormalizationError()


def _is_nse_equity_share(reference: DhanInstrumentReference) -> bool:
    """Return whether a master row is the NSE cash-equity anchor for stock derivatives."""
    instrument = reference.instrument
    return (
        instrument.exchange == "NSE"
        and reference.exchange_segment == "NSE_EQ"
        and reference.provider_instrument_type == "EQUITY"
        and instrument.market_segment is MarketSegment.EQUITY
        and instrument.instrument_class is InstrumentClass.CASH
        and instrument.listing_type == "ES"
    )


def _is_nse_stock_derivative_candidate(reference: DhanInstrumentReference) -> bool:
    """Return whether a raw master row claims to be an NSE stock derivative."""
    instrument = reference.instrument
    return (
        instrument.exchange == "NSE"
        and reference.exchange_segment == "NSE_FNO"
        and reference.provider_instrument_type in {"FUTSTK", "OPTSTK"}
        and instrument.underlying is not None
        and reference.underlying_security_id is not None
    )


def _is_production_nse_stock_derivative(
    reference: DhanInstrumentReference,
    cash_equities_by_security_id: Mapping[str, DhanInstrumentReference],
) -> bool:
    """Require each stock derivative to link to an NSE equity-share master record."""
    instrument = reference.instrument
    if not _is_nse_stock_derivative_candidate(reference):
        return False

    underlying_security_id = reference.underlying_security_id
    underlying = instrument.underlying
    if underlying_security_id is None or underlying is None:
        return False
    cash_equity = cash_equities_by_security_id.get(underlying_security_id)
    return cash_equity is not None and cash_equity.instrument.symbol == underlying.symbol


def _reference_sort_key(reference: DhanInstrumentReference) -> tuple[str, str, str, str, str]:
    instrument = reference.instrument
    return (
        instrument.exchange,
        instrument.symbol,
        instrument.instrument_class.value,
        instrument.expiry.isoformat() if instrument.expiry is not None else "",
        reference.security_id,
    )
