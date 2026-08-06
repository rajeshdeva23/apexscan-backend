"""Contract tests for direct asynchronous Dhan REST integration."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.base.broker_adapter import (
    BrokerAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
)
from app.schemas.market_data import (
    HistoricalRequest,
    Instrument,
    InstrumentClass,
    MarketSegment,
    OptionType,
    ProviderCapability,
    ProviderStatus,
    UnderlyingInstrument,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
_SECRET = "test-dhan-access-token-must-not-leak"
_RUNTIME_TOKEN = "test-dhan-runtime-token-must-not-leak"
_CLIENT_ID = "test-dhan-client-id-must-not-leak"
_PIN = "654321"
_TOTP_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_CASH_INSTRUMENT = Instrument(
    exchange="NSE",
    market_segment=MarketSegment.EQUITY,
    symbol="APEXCO",
    instrument_class=InstrumentClass.CASH,
    display_name="APEXCO",
    listing_type="ES",
    series="EQ",
)
_UNROUTABLE_COMMODITY_MASTER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,"
    "STRIKE_PRICE,OPTION_TYPE\n"
    "NSE,M,701,OPTFUT,501,GOLD,GOLD,GOLD OPTION,OPTFUT,1,1,2027-01-29,168500,CE\n"
)
_NSE_STOCK_FUTURES_MASTER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,"
    "STRIKE_PRICE,OPTION_TYPE\n"
    "NSE,D,201,FUTSTK,101,APEXCO,APEXCO30JANFUT,APEXCO JAN FUT,FUTSTK,NA,100,"
    "2030-01-31,0,\n"
    "NSE,D,202,FUTSTK,101,APEXCO,APEXCO30FEBFUT,APEXCO FEB FUT,FUTSTK,NA,100,"
    "2030-02-28,0,\n"
    "NSE,D,203,FUTSTK,101,APEXCO,APEXCO30MARFUT,APEXCO MAR FUT,FUTSTK,NA,100,"
    "2030-03-28,0,\n"
)


def _dhan() -> Any:
    """Import the Dhan implementation only after this test describes its contract."""
    try:
        return import_module("app.adapters.dhan")
    except ModuleNotFoundError:
        pytest.fail("P3.3 must expose a direct asynchronous Dhan REST adapter")


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


def _adapter(handler: httpx.AsyncByteStream | Any, **kwargs: object) -> Any:
    return _dhan().DhanRestAdapter(
        access_token=SecretStr(_SECRET),
        api_base_url="https://api.dhan.co/v2",
        timeout_seconds=3.0,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class _CountingTokenProvider:
    """Test-only wrapper that proves adapter teardown happens only at lifecycle end."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.disconnect_calls = 0

    async def get_access_token(self) -> SecretStr:
        return await self._delegate.get_access_token()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        await self._delegate.disconnect()


async def test_uses_documented_headers_and_daily_request_fields_without_sdk_injections() -> None:
    """Adding SDK-only client IDs to this documented request must fail this exact boundary."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))
        return httpx.Response(200, json=_json("daily_historical.json"))

    adapter = _adapter(handler)
    await adapter.connect()
    try:
        instruments = await adapter.load_instruments()
        result = await adapter.load_historical_data(_daily_request())
    finally:
        await adapter.disconnect()

    assert _CASH_INSTRUMENT in instruments
    assert result.candles[0].instrument == _CASH_INSTRUMENT
    reference_request, historical_request = requests
    assert reference_request.url == httpx.URL(
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    )
    assert reference_request.headers.get("access-token") is None
    assert historical_request.url == httpx.URL("https://api.dhan.co/v2/charts/historical")
    assert historical_request.headers["access-token"] == _SECRET
    assert historical_request.headers.get("client-id") is None
    assert json.loads(historical_request.content) == {
        "securityId": "101",
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "oi": False,
        "fromDate": "2025-01-01",
        "toDate": "2025-01-03",
    }
    assert "dhanClientId" not in historical_request.content.decode("utf-8")


async def test_constructs_documented_intraday_request_in_indian_market_time() -> None:
    """Formatting canonical UTC values without conversion to market time must fail this request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))
        return httpx.Response(200, json=_json("intraday_historical.json"))

    request = HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, 3, 45, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 1, 4, 0, tzinfo=UTC),
        interval=timedelta(minutes=15),
    )
    adapter = _adapter(handler)
    await adapter.connect()
    try:
        await adapter.load_instruments()
        result = await adapter.load_historical_data(request)
    finally:
        await adapter.disconnect()

    assert result.candles[0].start_timestamp == datetime(2025, 1, 1, 3, 45, tzinfo=UTC)
    assert requests[1].url == httpx.URL("https://api.dhan.co/v2/charts/intraday")
    assert json.loads(requests[1].content) == {
        "securityId": "101",
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "interval": "15",
        "oi": False,
        "fromDate": "2025-01-01 09:15:00",
        "toDate": "2025-01-01 09:30:00",
    }


async def test_sends_documented_expiry_codes_for_near_next_and_far_stock_futures() -> None:
    """Omitting or mis-ranking a FUTSTK expiry would make the daily request ambiguous."""
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_NSE_STOCK_FUTURES_MASTER)
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_json("daily_historical.json"))

    adapter = _adapter(handler)
    await adapter.connect()
    try:
        instruments = await adapter.load_instruments()
        futures = sorted(
            (
                instrument
                for instrument in instruments
                if instrument.instrument_class is InstrumentClass.FUTURE
            ),
            key=lambda instrument: instrument.expiry,
        )
        for instrument in futures:
            await adapter.load_historical_data(
                HistoricalRequest(
                    instrument=instrument,
                    start_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    end_timestamp=datetime(2026, 1, 3, tzinfo=UTC),
                    interval=timedelta(days=1),
                )
            )
    finally:
        await adapter.disconnect()

    assert [payload["expiryCode"] for payload in payloads] == [0, 1, 2]


async def test_single_adapter_lifecycle_reuses_one_token_for_profile_and_history(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A protected smoke sequence must not create a second token before teardown."""
    dhan = _dhan()
    authentication_requests = 0
    api_token_headers: list[str | None] = []
    public_master_token_headers: list[str | None] = []
    api_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal authentication_requests
        if request.url.host == "auth.dhan.co":
            authentication_requests += 1
            return httpx.Response(
                200,
                json={
                    "accessToken": _RUNTIME_TOKEN,
                    "expiryTime": "2030-01-01T00:00:00.000",
                },
            )
        if request.url.host == "images.dhan.co":
            public_master_token_headers.append(request.headers.get("access-token"))
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))

        api_paths.append(request.url.path)
        api_token_headers.append(request.headers.get("access-token"))
        if request.url.path == "/v2/profile":
            return httpx.Response(200, json={"tokenValidity": "fixture-only"})
        if request.url.path == "/v2/charts/historical":
            return httpx.Response(200, json=_json("daily_historical.json"))
        if request.url.path == "/v2/charts/intraday":
            return httpx.Response(200, json=_json("intraday_historical.json"))
        pytest.fail(f"unexpected request path: {request.url.path}")

    caplog.set_level(logging.INFO, logger="httpx")
    transport = httpx.MockTransport(handler)
    manager = dhan.DhanAuthManager(
        client_id=SecretStr(_CLIENT_ID),
        pin=SecretStr(_PIN),
        totp_secret=SecretStr(_TOTP_SECRET),
        timeout_seconds=3.0,
        transport=transport,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    provider = _CountingTokenProvider(manager)
    adapter = dhan.DhanRestAdapter(
        token_provider=provider,
        api_base_url="https://api.dhan.co/v2",
        timeout_seconds=3.0,
        transport=transport,
        request_pacer=dhan.DhanRequestPacer(minimum_interval_seconds=0),
    )
    intraday_request = HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, 3, 45, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 1, 4, 0, tzinfo=UTC),
        interval=timedelta(minutes=5),
    )

    await adapter.connect()
    try:
        health = await adapter.get_health()
        instruments = await adapter.load_instruments()
        universe = adapter.load_fno_stock_universe()
        daily = await adapter.load_historical_data(_daily_request())
        intraday = await adapter.load_historical_data(intraday_request)
    finally:
        await adapter.disconnect()

    assert health.status is ProviderStatus.HEALTHY
    assert _CASH_INSTRUMENT in instruments
    assert universe.futures
    assert universe.options
    assert daily.candles
    assert intraday.candles
    assert authentication_requests == 1
    assert public_master_token_headers == [None]
    assert api_paths == ["/v2/profile", "/v2/charts/historical", "/v2/charts/intraday"]
    assert all(header == api_token_headers[0] for header in api_token_headers)
    assert api_token_headers[0] is not None
    assert provider.disconnect_calls == 1
    assert manager.current_token_expires_at is None
    for sensitive in (_CLIENT_ID, _PIN, _TOTP_SECRET, _RUNTIME_TOKEN):
        assert sensitive not in caplog.text


async def test_rejects_unsupported_intraday_interval_before_a_dhan_request() -> None:
    """Sending an undocumented two-minute interval would violate the endpoint contract."""
    dhan = _dhan()

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url}")

    adapter = _adapter(handler)
    request = HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 1, 0, 2, tzinfo=UTC),
        interval=timedelta(minutes=2),
    )
    await adapter.connect()
    try:
        with pytest.raises(dhan.UnsupportedProviderRequestError):
            await adapter.load_historical_data(request)
    finally:
        await adapter.disconnect()


async def test_rejects_an_undocumented_provider_route_before_a_historical_request() -> None:
    """Sending a guessed provider route would violate the adapter boundary."""
    dhan = _dhan()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_UNROUTABLE_COMMODITY_MASTER)
        pytest.fail(f"unexpected provider request to {request.url}")

    request = HistoricalRequest(
        instrument=Instrument(
            exchange="NSE",
            market_segment=MarketSegment.COMMODITY,
            symbol="GOLD",
            instrument_class=InstrumentClass.OPTION,
            underlying=UnderlyingInstrument(exchange="NSE", symbol="GOLD"),
            listing_type="OPTFUT",
            series="1",
            expiry=date(2027, 1, 29),
            strike_price=Decimal("168500"),
            option_type=OptionType.CALL,
        ),
        start_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 3, tzinfo=UTC),
        interval=timedelta(days=1),
    )
    adapter = _adapter(handler)
    await adapter.connect()
    try:
        await adapter.load_instruments()
        with pytest.raises(dhan.UnsupportedProviderRequestError):
            await adapter.load_historical_data(request)
    finally:
        await adapter.disconnect()

    assert [item.url.host for item in requests] == ["images.dhan.co"]


async def test_translates_documented_dhan_rate_limit_response_without_secret_leakage() -> None:
    """Treating DH-904 as a generic result would hide required caller back-pressure."""
    dhan = _dhan()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))
        return httpx.Response(429, json=_json("rate_limit_error.json"))

    adapter = _adapter(handler)
    await adapter.connect()
    try:
        await adapter.load_instruments()
        with pytest.raises(dhan.ProviderRateLimitError) as captured:
            await adapter.load_historical_data(_daily_request())
    finally:
        await adapter.disconnect()

    assert _SECRET not in str(captured.value)


async def test_translates_http_timeout_to_provider_independent_error() -> None:
    """Leaking an HTTPX timeout above the adapter would couple callers to the transport."""
    dhan = _dhan()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout", request=request)

    adapter = _adapter(handler)
    await adapter.connect()
    try:
        with pytest.raises(dhan.ProviderTimeoutError) as captured:
            await adapter.get_health()
    finally:
        await adapter.disconnect()

    assert _SECRET not in str(captured.value)


async def test_partitions_intraday_requests_at_the_documented_ninety_day_limit() -> None:
    """Sending a 91-day minute range as one call would violate Dhan's documented limit."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))
        return httpx.Response(200, json=_json("intraday_historical.json"))

    request = HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        end_timestamp=datetime(2025, 4, 2, tzinfo=UTC),
        interval=timedelta(minutes=5),
    )
    adapter = _adapter(handler)
    await adapter.connect()
    try:
        await adapter.load_instruments()
        result = await adapter.load_historical_data(request)
    finally:
        await adapter.disconnect()

    historical_requests = requests[1:]
    assert len(historical_requests) == 2
    assert json.loads(historical_requests[0].content)["toDate"] == "2025-04-01 05:30:00"
    assert json.loads(historical_requests[1].content)["fromDate"] == "2025-04-01 05:30:00"
    assert len(result.candles) == 2


async def test_paces_successive_data_api_requests_at_the_documented_rate() -> None:
    """Dropping the rate pacer would make this second request immediately eligible."""
    dhan = _dhan()
    times = iter((10.0, 10.0, 10.0, 10.2))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    pacer = dhan.DhanRequestPacer(clock=lambda: next(times), sleep=record_sleep)

    await pacer.wait()
    await pacer.wait()

    assert delays == [0.2]


async def test_live_smoke_reports_undocumented_client_id_requirement_safely() -> None:
    """Copying the SDK's client-ID injection after a live error must remain prohibited."""
    dhan = _dhan()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))
        return httpx.Response(
            401,
            json={
                "errorType": "Data API Error",
                "errorCode": "810",
                "errorMessage": "dhanClientId is mandatory for this endpoint",
            },
        )

    adapter = _adapter(handler, live_smoke_enabled=True)
    await adapter.connect()
    try:
        await adapter.load_instruments()
        with pytest.raises(dhan.DhanRestContractDiscrepancyError) as captured:
            await adapter.load_historical_data(_daily_request())
    finally:
        await adapter.disconnect()

    error = captured.value
    assert error.endpoint == "/charts/historical"
    assert error.http_status == 401
    assert error.error_code == "810"
    assert error.observed_requirement == "dhanClientId"
    assert _SECRET not in str(error)


async def test_implements_the_authorized_broker_neutral_capabilities() -> None:
    """The P3.4 standard feed must be advertised alongside the existing P3.3 capabilities."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tokenValidity": "fixture-only"})

    adapter = _adapter(handler)

    assert isinstance(adapter, BrokerAdapter)
    assert isinstance(adapter, HistoricalDataAdapter)
    assert isinstance(adapter, InstrumentDataAdapter)
    assert adapter.capabilities == frozenset(
        {
            ProviderCapability.HISTORICAL_DATA,
            ProviderCapability.INSTRUMENTS,
            ProviderCapability.LIVE_MARKET_DATA,
        }
    )

    await adapter.connect()
    try:
        health = await adapter.get_health()
    finally:
        await adapter.disconnect()

    assert health.status is ProviderStatus.HEALTHY
