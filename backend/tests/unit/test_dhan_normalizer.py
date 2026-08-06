"""Fixture-based tests for the Dhan-to-canonical normalization boundary."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from app.adapters.base.errors import NormalizationError
from app.schemas.market_data import (
    HistoricalRequest,
    Instrument,
    InstrumentClass,
    MarketSegment,
    UnderlyingInstrument,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
_CASH_INSTRUMENT = Instrument(
    exchange="NSE",
    market_segment=MarketSegment.EQUITY,
    symbol="APEXCO",
    instrument_class=InstrumentClass.CASH,
    display_name="APEXCO",
    listing_type="ES",
    series="EQ",
)


def _dhan() -> Any:
    """Import the concrete adapter namespace or fail with the P3.3 contract."""
    try:
        return import_module("app.adapters.dhan")
    except ModuleNotFoundError:
        pytest.fail("P3.3 must provide the Dhan adapter namespace implementation")


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _json(name: str) -> dict[str, object]:
    return json.loads(_text(name))


def _daily_request() -> HistoricalRequest:
    return HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 3, tzinfo=UTC),
        interval=timedelta(days=1),
    )


def test_normalizes_documented_instrument_master_rows_to_canonical_identity() -> None:
    """Removing a required documented reference field must reject the master safely."""
    dhan = _dhan()

    references = dhan.normalize_instrument_master(_text("instrument_master_detailed.csv"))

    assert references[0].instrument == _CASH_INSTRUMENT
    assert references[0].security_id == "101"
    assert references[0].exchange_segment == "NSE_EQ"
    assert references[1].instrument == Instrument(
        exchange="NSE",
        market_segment=MarketSegment.DERIVATIVES,
        symbol="APEXCO26AUGFUT",
        instrument_class=InstrumentClass.FUTURE,
        underlying=UnderlyingInstrument(exchange="NSE", symbol="APEXCO"),
        display_name="APEXCO FUT",
        listing_type="FUTSTK",
        expiry=date(2026, 8, 27),
    )
    assert all(isinstance(reference.instrument, Instrument) for reference in references)


def test_derives_stock_futures_options_and_underlyings_without_index_mixing() -> None:
    """Classifying FUTIDX as a stock future must fail this equity-universe boundary."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(_text("instrument_master_detailed.csv"))

    universe = dhan.derive_equity_fno_universe(references)

    assert tuple(reference.security_id for reference in universe.futures) == ("201",)
    assert tuple(reference.security_id for reference in universe.options) == ("202",)
    assert universe.underlyings == (UnderlyingInstrument(exchange="NSE", symbol="APEXCO"),)


def test_derives_production_nse_equity_fno_universe_from_linked_cash_instrument_type() -> None:
    """Including non-equity linked cash rows would leak reference contracts into scanners."""
    dhan = _dhan()
    production_master = _text("instrument_master_production_universe.csv")

    references = dhan.normalize_instrument_master(production_master)
    universe = dhan.derive_equity_fno_universe(references)

    assert tuple(reference.instrument.symbol for reference in universe.futures) == (
        "360ONE26AUGFUT",
        "360ONE26SEPFUT",
        "ABCAPITAL26AUGFUT",
    )
    assert tuple(reference.instrument.symbol for reference in universe.options) == (
        "ABB26AUG4200CE",
        "ABB26AUG4200PE",
    )
    assert universe.underlyings == (
        UnderlyingInstrument(exchange="NSE", symbol="360ONE"),
        UnderlyingInstrument(exchange="NSE", symbol="ABB"),
        UnderlyingInstrument(exchange="NSE", symbol="ABCAPITAL"),
    )
    assert all(
        reference.instrument.underlying.symbol != "011NSETEST" for reference in universe.futures
    )
    assert all(
        reference.provider_instrument_type in {"FUTSTK", "OPTSTK"}
        for reference in (*universe.futures, *universe.options)
    )


def test_resolves_one_nse_cash_equity_reference_for_each_validated_underlying() -> None:
    """Replacing a structural cash mapping with a derivative reference must fail this gate."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )

    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)

    assert tuple(reference.instrument.symbol for reference in live_universe.cash_references) == (
        "360ONE",
        "ABB",
        "ABCAPITAL",
    )
    assert live_universe.missing_underlyings == ()
    assert live_universe.ambiguous_underlyings == ()
    assert live_universe.symbol_mismatches == ()


def test_cash_equity_mapping_gate_reports_ambiguous_underlying_without_choosing_one() -> None:
    """Choosing either of two structural cash references would subscribe an arbitrary instrument."""
    dhan = _dhan()
    master = _text("instrument_master_production_universe.csv")
    master += "NSE,E,105,EQUITY,,,360ONE,360 ONE WAM LIMITED,ES,BE,1,,,\n"
    master += "NSE,D,203,FUTSTK,105,360ONE,360ONE26OCTFUT,360ONE OCT FUT,FUT,NA,500,2026-10-29,0,\n"

    live_universe = dhan.resolve_nse_cash_equity_live_universe(
        dhan.normalize_instrument_master(master)
    )

    assert tuple(reference.instrument.symbol for reference in live_universe.cash_references) == (
        "ABB",
        "ABCAPITAL",
    )
    assert live_universe.ambiguous_underlyings == (
        UnderlyingInstrument(exchange="NSE", symbol="360ONE"),
    )


def test_cash_equity_mapping_gate_reports_missing_underlying_without_dropping_the_diagnostic() -> (
    None
):
    """Silently omitting a derivative with no cash reference would make the universe incomplete."""
    dhan = _dhan()
    master = _text("instrument_master_production_universe.csv")
    master += (
        "NSE,D,999,FUTSTK,998,APEXMISSING,APEXMISSING26AUGFUT,APEX MISSING FUT,"
        "FUT,NA,1,2026-08-27,0,\n"
    )

    live_universe = dhan.resolve_nse_cash_equity_live_universe(
        dhan.normalize_instrument_master(master)
    )

    assert live_universe.missing_underlyings == (
        UnderlyingInstrument(exchange="NSE", symbol="APEXMISSING"),
    )
    assert all(
        reference.instrument.symbol != "APEXMISSING" for reference in live_universe.cash_references
    )


def test_cash_equity_mapping_gate_reports_symbol_mismatch_without_guessing_a_cash_reference() -> (
    None
):
    """Using a cash row with a different symbol would corrupt canonical underlying identity."""
    dhan = _dhan()
    master = _text("instrument_master_production_universe.csv")
    master += "NSE,E,105,EQUITY,,,MAPPED,WRONG CASH ROW,ES,EQ,1,,,\n"
    master += (
        "NSE,D,999,FUTSTK,105,APEXMISMATCH,APEXMISMATCH26AUGFUT,APEX MISMATCH FUT,"
        "FUT,NA,1,2026-08-27,0,\n"
    )

    live_universe = dhan.resolve_nse_cash_equity_live_universe(
        dhan.normalize_instrument_master(master)
    )

    assert live_universe.symbol_mismatches == (
        UnderlyingInstrument(exchange="NSE", symbol="APEXMISMATCH"),
    )
    assert all(
        reference.instrument.symbol != "MAPPED" for reference in live_universe.cash_references
    )


def test_rejects_ambiguous_duplicate_canonical_instrument_references() -> None:
    """Selecting one of two security IDs for a canonical instrument must never be implicit."""
    dhan = _dhan()
    duplicate = "NSE,E,999,EQUITY,,,APEXCO,APEXCO,ES,EQ,1\n"

    with pytest.raises(NormalizationError) as captured:
        dhan.normalize_instrument_master(_text("instrument_master_detailed.csv") + duplicate)

    assert "999" not in str(captured.value)


def test_normalizes_daily_ohlcv_and_epoch_timestamps_to_utc_candles() -> None:
    """Using local or naive timestamps would break this canonical UTC result."""
    dhan = _dhan()

    result = dhan.normalize_historical_payload(_daily_request(), _json("daily_historical.json"))

    assert result.request == _daily_request()
    assert result.candles[0].start_timestamp == datetime(2025, 1, 1, tzinfo=UTC)
    assert result.candles[0].end_timestamp == datetime(2025, 1, 2, tzinfo=UTC)
    assert str(result.candles[0].open_price) == "100.25"
    assert result.candles[0].traded_quantity == 1250


def test_normalizes_intraday_epoch_timestamp_as_utc() -> None:
    """Interpreting Dhan epoch seconds as local wall time must fail this assertion."""
    dhan = _dhan()
    request = HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, 3, 45, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 1, 4, 0, tzinfo=UTC),
        interval=timedelta(minutes=15),
    )

    result = dhan.normalize_historical_payload(request, _json("intraday_historical.json"))

    assert result.candles[0].start_timestamp == datetime(2025, 1, 1, 3, 45, tzinfo=UTC)
    assert result.candles[0].end_timestamp == datetime(2025, 1, 1, 4, 0, tzinfo=UTC)


def test_normalizes_whole_number_float_epochs_to_the_same_utc_timestamp_as_integers() -> None:
    """Rejecting Dhan's integral JSON-float epoch would discard valid historical data."""
    dhan = _dhan()
    integer_result = dhan.normalize_historical_payload(
        _daily_request(), _json("daily_historical.json")
    )
    float_result = dhan.normalize_historical_payload(
        _daily_request(),
        _json("whole_number_float_historical.json"),
    )

    assert integer_result.candles[0].start_timestamp == datetime(2025, 1, 1, tzinfo=UTC)
    assert float_result.candles[0].start_timestamp == integer_result.candles[0].start_timestamp
    assert float_result.candles[1].start_timestamp == integer_result.candles[1].start_timestamp


def test_normalizes_whole_number_float_volumes_to_the_same_integer_quantities() -> None:
    """Rejecting Dhan's integral JSON-float quantities would discard valid candles."""
    dhan = _dhan()
    integer_result = dhan.normalize_historical_payload(
        _daily_request(), _json("daily_historical.json")
    )
    float_result = dhan.normalize_historical_payload(
        _daily_request(),
        _json("whole_number_float_timestamp_and_volume_historical.json"),
    )

    assert integer_result.candles[0].traded_quantity == 1250
    assert float_result.candles[0].traded_quantity == integer_result.candles[0].traded_quantity
    assert float_result.candles[1].traded_quantity == integer_result.candles[1].traded_quantity


@pytest.mark.parametrize(
    "volume",
    [1250.5, -1, -1.0, math.nan, math.inf, -math.inf, True, "1250"],
)
def test_rejects_non_integral_or_invalid_historical_volumes(volume: object) -> None:
    """Coercing fractional, invalid, or negative Dhan quantities would corrupt volume semantics."""
    dhan = _dhan()
    payload = _json("daily_historical.json")
    payload["volume"] = [volume, 1450]

    with pytest.raises(NormalizationError):
        dhan.normalize_historical_payload(_daily_request(), payload)


@pytest.mark.parametrize("volume", [0, 0.0])
def test_normalizes_zero_historical_volume_consistently(volume: object) -> None:
    """A valid zero traded quantity must not depend on Dhan's JSON number representation."""
    dhan = _dhan()
    payload = _json("daily_historical.json")
    payload["volume"] = [volume, 1450]

    result = dhan.normalize_historical_payload(_daily_request(), payload)

    assert result.candles[0].traded_quantity == 0


@pytest.mark.parametrize(
    "timestamp",
    [1735689600.5, -1735689600.5, math.nan, math.inf, -math.inf, True],
)
def test_rejects_non_integral_or_non_finite_historical_timestamps(timestamp: object) -> None:
    """Rounding fractional or non-finite provider epochs would fabricate candle times."""
    dhan = _dhan()
    payload = _json("daily_historical.json")
    payload["timestamp"] = [timestamp, 1735776000]

    with pytest.raises(NormalizationError):
        dhan.normalize_historical_payload(_daily_request(), payload)


def test_rejects_malformed_historical_timestamp_without_payload_leakage() -> None:
    """A malformed timestamp must not expose unrelated provider payload fields."""
    dhan = _dhan()
    secret = "fixture-secret-must-not-leak"
    payload = _json("daily_historical.json")
    payload["timestamp"] = ["not-an-epoch", 1735776000]
    payload["authorization"] = secret

    with pytest.raises(NormalizationError) as captured:
        dhan.normalize_historical_payload(_daily_request(), payload)

    assert secret not in str(captured.value)


def test_rejects_misaligned_historical_arrays_without_exposing_payload_values() -> None:
    """Accepting a partial OHLCV array would create fabricated candle fields."""
    dhan = _dhan()
    secret = "fixture-secret-must-not-leak"
    malformed = _json("malformed_historical.json")
    malformed["authorization"] = secret

    with pytest.raises(NormalizationError) as captured:
        dhan.normalize_historical_payload(_daily_request(), malformed)

    assert secret not in str(captured.value)
