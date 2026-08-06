"""Private Dhan transport/reference values kept below the adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.market_data import Instrument, UnderlyingInstrument


@dataclass(frozen=True, slots=True)
class DhanInstrumentReference:
    """A documented Dhan reference row linked to a canonical instrument."""

    instrument: Instrument
    security_id: str
    underlying_security_id: str | None
    exchange_segment: str | None
    provider_instrument_type: str


@dataclass(frozen=True, slots=True)
class DhanFnoStockUniverse:
    """Dhan stock-derivative references and their distinct equity underlyings."""

    futures: tuple[DhanInstrumentReference, ...]
    options: tuple[DhanInstrumentReference, ...]
    underlyings: tuple[UnderlyingInstrument, ...]


@dataclass(frozen=True, slots=True)
class DhanCashEquityLiveUniverse:
    """Private cash-equity references resolved for the F&O-eligible scanner universe."""

    underlyings: tuple[UnderlyingInstrument, ...]
    cash_references: tuple[DhanInstrumentReference, ...]
    missing_underlyings: tuple[UnderlyingInstrument, ...]
    ambiguous_underlyings: tuple[UnderlyingInstrument, ...]
    symbol_mismatches: tuple[UnderlyingInstrument, ...]
