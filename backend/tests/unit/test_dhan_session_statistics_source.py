"""Dhan Market Quote session-statistics source: mapping + batch request (P4.6E3; ADR-009).

Pure normalization tests use fixture payloads; adapter tests use an injected
``httpx.MockTransport`` — never the network, never real credentials.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.base import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    SessionStatisticsSource,
    UnsupportedProviderRequestError,
)
from app.adapters.base.errors import NormalizationError
from app.adapters.dhan import DhanRestAdapter, normalize_session_statistics_payload
from app.adapters.dhan.models import DhanInstrumentReference
from app.schemas.market_data import Instrument, ProviderCapability, SessionStatisticsObservation

_DATE = date(2026, 8, 11)
_AT = datetime(2026, 8, 11, 6, 30, tzinfo=UTC)
_CLIENT_ID = SecretStr("cid")


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _reference(
    instrument: Instrument, security_id: str, *, segment: str | None = "NSE_EQ"
) -> DhanInstrumentReference:
    return DhanInstrumentReference(
        instrument=instrument,
        security_id=security_id,
        underlying_security_id=None,
        exchange_segment=segment,
        provider_instrument_type="EQUITY",
    )


def _ohlc_item(
    open_: float | int, high: float | int, low: float | int, close: float | int
) -> dict[str, object]:
    return {"last_price": close, "ohlc": {"open": open_, "high": high, "low": low, "close": close}}


def _response(data: dict[str, object], *, status: str = "success") -> dict[str, object]:
    return {"data": data, "status": status}


# --------------------------------------------------------------------------- #
# Pure normalization
# --------------------------------------------------------------------------- #
def test_valid_single_instrument_maps_to_observation() -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "11536"))]
    payload = _response({"NSE_EQ": {"11536": _ohlc_item(100, 105, 98, 101)}})
    obs = normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
    assert len(obs) == 1
    assert obs[0].instrument == inst
    assert obs[0].trading_date == _DATE
    assert obs[0].observed_at == _AT
    ohlc = obs[0].session_ohlc
    assert (ohlc.open_price, ohlc.high_price, ohlc.low_price, ohlc.close_price) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("98"),
        Decimal("101"),
    )


def test_decimal_normalization_from_provider_floats() -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "1"))]
    payload = _response({"NSE_EQ": {"1": _ohlc_item(4521.45, 4530, 4500, 4507.85)}})
    obs = normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
    assert obs[0].session_ohlc.open_price == Decimal("4521.45")
    assert obs[0].session_ohlc.high_price == Decimal("4530")


def test_multiple_instruments_and_deterministic_order_independent_of_response() -> None:
    reliance, tcs = _instrument("RELIANCE"), _instrument("TCS")
    pairs = [(reliance, _reference(reliance, "1")), (tcs, _reference(tcs, "2"))]
    # Response lists TCS before RELIANCE; output must still follow pairs (canonical) order.
    payload = _response(
        {"NSE_EQ": {"2": _ohlc_item(200, 210, 195, 205), "1": _ohlc_item(100, 105, 98, 101)}}
    )
    obs = normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
    assert [o.instrument.symbol for o in obs] == ["RELIANCE", "TCS"]


def test_one_shared_observed_at_for_the_whole_batch() -> None:
    reliance, tcs = _instrument("RELIANCE"), _instrument("TCS")
    pairs = [(reliance, _reference(reliance, "1")), (tcs, _reference(tcs, "2"))]
    payload = _response(
        {"NSE_EQ": {"1": _ohlc_item(100, 105, 98, 101), "2": _ohlc_item(200, 210, 195, 205)}}
    )
    obs = normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
    assert {o.observed_at for o in obs} == {_AT}


@pytest.mark.parametrize(
    ("open_", "high", "low", "close"),
    [
        (0, 0, 0, 0),  # sentinel/uninitialised
        (100, 98, 99, 100),  # high < low
        (110, 105, 98, 101),  # open above high
        (100, 105, 98, 120),  # close above high
    ],
)
def test_malformed_or_sentinel_ohlc_is_withheld(
    open_: float, high: float, low: float, close: float
) -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "1"))]
    payload = _response({"NSE_EQ": {"1": _ohlc_item(open_, high, low, close)}})
    assert (
        normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
        == ()
    )


def test_missing_instrument_is_withheld() -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "1"))]
    payload = _response({"NSE_EQ": {}})  # requested id absent
    assert (
        normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
        == ()
    )


def test_one_malformed_instrument_does_not_poison_valid_others() -> None:
    reliance, tcs = _instrument("RELIANCE"), _instrument("TCS")
    pairs = [(reliance, _reference(reliance, "1")), (tcs, _reference(tcs, "2"))]
    payload = _response(
        {"NSE_EQ": {"1": _ohlc_item(100, 105, 98, 101), "2": _ohlc_item(0, 0, 0, 0)}}
    )
    obs = normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
    assert [o.instrument.symbol for o in obs] == ["RELIANCE"]


def test_non_success_status_is_rejected() -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "1"))]
    with pytest.raises(NormalizationError):
        normalize_session_statistics_payload(
            _response({"NSE_EQ": {"1": _ohlc_item(100, 105, 98, 101)}}, status="failed"),
            pairs,
            trading_date=_DATE,
            observed_at=_AT,
        )


def test_structurally_invalid_outer_payload_is_rejected() -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "1"))]
    with pytest.raises(NormalizationError):
        normalize_session_statistics_payload(
            {"status": "success", "data": "not-a-mapping"},
            pairs,
            trading_date=_DATE,
            observed_at=_AT,
        )


def test_empty_pairs_yields_no_observations() -> None:
    assert (
        normalize_session_statistics_payload(_response({}), [], trading_date=_DATE, observed_at=_AT)
        == ()
    )


def test_observation_serialization_carries_no_provider_fields() -> None:
    inst = _instrument()
    pairs = [(inst, _reference(inst, "11536"))]
    payload = _response({"NSE_EQ": {"11536": _ohlc_item(100, 105, 98, 101)}})
    obs = normalize_session_statistics_payload(payload, pairs, trading_date=_DATE, observed_at=_AT)
    dumped = json.dumps(obs[0].model_dump(mode="json"))
    for forbidden in ("11536", "NSE_EQ", "security_id", "exchange_segment", "day_open"):
        assert forbidden not in dumped


# --------------------------------------------------------------------------- #
# Adapter integration (injected MockTransport — no network, no real credentials)
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self, response: dict[str, object]) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self._response)


async def _adapter(
    recorder: _Recorder, *, client_id: SecretStr | None = _CLIENT_ID
) -> DhanRestAdapter:
    adapter = DhanRestAdapter(
        access_token=SecretStr("token"),
        transport=httpx.MockTransport(recorder),
        live_client_id=client_id,
    )
    await adapter.connect()
    return adapter


def _install_references(
    adapter: DhanRestAdapter, references: dict[Instrument, DhanInstrumentReference]
) -> None:
    adapter._references = references  # noqa: SLF001 (white-box: bypass CSV master fetch)


async def test_source_protocol_and_capability() -> None:
    recorder = _Recorder(_response({}))
    adapter = await _adapter(recorder)
    assert isinstance(adapter, SessionStatisticsSource)
    assert ProviderCapability.MARKET_QUOTE in adapter.capabilities


async def test_adapter_loads_and_maps_one_instrument() -> None:
    inst = _instrument()
    recorder = _Recorder(_response({"NSE_EQ": {"11536": _ohlc_item(100, 105, 98, 101)}}))
    adapter = await _adapter(recorder)
    _install_references(adapter, {inst: _reference(inst, "11536")})
    obs = await adapter.load_session_statistics([inst], trading_date=_DATE, observed_at=_AT)
    assert isinstance(obs[0], SessionStatisticsObservation)
    assert obs[0].session_ohlc.high_price == Decimal("105")
    # Exactly one POST to the documented OHLC endpoint, carrying the client-id header.
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/marketfeed/ohlc")
    assert request.headers["client-id"] == "cid"


async def test_full_universe_fits_in_one_batch_request() -> None:
    instruments = [_instrument(f"SYM{i:03d}") for i in range(208)]
    references = {inst: _reference(inst, str(1000 + i)) for i, inst in enumerate(instruments)}
    data = {"NSE_EQ": {str(1000 + i): _ohlc_item(100, 105, 98, 101) for i in range(208)}}
    recorder = _Recorder(_response(data))
    adapter = await _adapter(recorder)
    _install_references(adapter, references)
    obs = await adapter.load_session_statistics(instruments, trading_date=_DATE, observed_at=_AT)
    assert len(obs) == 208
    assert len(recorder.requests) == 1  # single batch — no per-instrument request storm
    body = json.loads(recorder.requests[0].content)
    assert len(body["NSE_EQ"]) == 208


async def test_empty_instrument_collection_makes_no_request() -> None:
    recorder = _Recorder(_response({}))
    adapter = await _adapter(recorder)
    assert await adapter.load_session_statistics([], trading_date=_DATE, observed_at=_AT) == ()
    assert recorder.requests == []


async def test_duplicate_requested_instruments_are_deduplicated() -> None:
    inst = _instrument()
    recorder = _Recorder(_response({"NSE_EQ": {"1": _ohlc_item(100, 105, 98, 101)}}))
    adapter = await _adapter(recorder)
    _install_references(adapter, {inst: _reference(inst, "1")})
    obs = await adapter.load_session_statistics([inst, inst], trading_date=_DATE, observed_at=_AT)
    assert len(obs) == 1
    assert len(json.loads(recorder.requests[0].content)["NSE_EQ"]) == 1


async def test_unmapped_instrument_fails_closed() -> None:
    recorder = _Recorder(_response({}))
    adapter = await _adapter(recorder)
    _install_references(adapter, {})  # nothing resolvable
    with pytest.raises(UnsupportedProviderRequestError):
        await adapter.load_session_statistics([_instrument()], trading_date=_DATE, observed_at=_AT)
    assert recorder.requests == []


async def test_missing_client_id_fails_closed() -> None:
    inst = _instrument()
    recorder = _Recorder(_response({}))
    adapter = await _adapter(recorder, client_id=None)
    _install_references(adapter, {inst: _reference(inst, "1")})
    with pytest.raises(ProviderAuthenticationError):
        await adapter.load_session_statistics([inst], trading_date=_DATE, observed_at=_AT)
    assert recorder.requests == []


async def test_rate_limit_error_is_translated() -> None:
    inst = _instrument()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"errorType": "rate", "errorMessage": "slow down"})

    adapter = DhanRestAdapter(
        access_token=SecretStr("token"),
        transport=httpx.MockTransport(handler),
        live_client_id=SecretStr("cid"),
    )
    await adapter.connect()
    _install_references(adapter, {inst: _reference(inst, "1")})
    with pytest.raises(ProviderRateLimitError):
        await adapter.load_session_statistics([inst], trading_date=_DATE, observed_at=_AT)


async def test_determinism_same_inputs_same_observations() -> None:
    inst = _instrument()
    references = {inst: _reference(inst, "11536")}
    data = _response({"NSE_EQ": {"11536": _ohlc_item(100, 105, 98, 101)}})

    async def run() -> tuple[SessionStatisticsObservation, ...]:
        adapter = await _adapter(_Recorder(data))
        _install_references(adapter, references)
        return await adapter.load_session_statistics([inst], trading_date=_DATE, observed_at=_AT)

    assert await run() == await run()
