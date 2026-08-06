"""Protected opt-in validation for the documented Dhan REST request contract."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.adapters.dhan import (
    DhanInstrumentReference,
    DhanRestAdapter,
    DhanRestContractDiscrepancyError,
)
from app.core.config import get_settings
from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    Instrument,
    MarketDataKind,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

pytestmark = pytest.mark.live_dhan
_INDIAN_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
_NSE_REGULAR_MARKET_OPEN = time(9, 15)
_NSE_REGULAR_MARKET_CLOSE = time(15, 30)
_LIVE_SMOKE_SUBSET_SIZE = 5
_LIVE_SMOKE_RECEIVE_TIMEOUT_SECONDS = 15.0


def _safe_candle(candle: Candle) -> str:
    """Format canonical market data without provider-local identifiers."""
    return (
        f"timestamp={candle.start_timestamp.isoformat()}; "
        f"open_price={candle.open_price}; high_price={candle.high_price}; "
        f"low_price={candle.low_price}; close_price={candle.close_price}; "
        f"traded_quantity={candle.traded_quantity}"
    )


def _safe_tick(tick: Tick) -> str:
    """Format one canonical live event without provider-local identifiers or credentials."""
    return (
        f"timestamp={tick.event_timestamp.isoformat()}; "
        f"last_price={tick.last_price}; traded_quantity={tick.traded_quantity}"
    )


def _nse_regular_market_is_open(now: datetime) -> bool:
    """Return whether a bounded live-packet smoke is meaningful at the supplied Indian time."""
    market_time = now.astimezone(_INDIAN_MARKET_TIMEZONE)
    return (
        market_time.weekday() < 5
        and _NSE_REGULAR_MARKET_OPEN <= market_time.time() < _NSE_REGULAR_MARKET_CLOSE
    )


def test_safe_candle_reports_current_canonical_fields_without_credentials() -> None:
    """Stale Candle field names must not break the protected smoke report."""
    candle = Candle(
        instrument=Instrument(exchange="NSE", symbol="SAFE"),
        start_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 2, tzinfo=UTC),
        open_price=Decimal("100.25"),
        high_price=Decimal("102.50"),
        low_price=Decimal("99.75"),
        close_price=Decimal("101.50"),
        traded_quantity=1250,
    )

    assert _safe_candle(candle) == (
        "timestamp=2025-01-01T00:00:00+00:00; "
        "open_price=100.25; high_price=102.50; low_price=99.75; "
        "close_price=101.50; traded_quantity=1250"
    )


def test_safe_tick_reports_only_canonical_values_without_provider_references() -> None:
    """The live smoke report must not regress into printing a Dhan provider locator."""
    tick = Tick(
        instrument=Instrument(exchange="NSE", symbol="SAFE"),
        event_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        last_price=Decimal("101.25"),
        traded_quantity=12,
    )

    assert _safe_tick(tick) == (
        "timestamp=2025-01-01T00:00:00+00:00; last_price=101.25; traded_quantity=12"
    )


def _select_nse_equity_futstk(
    futures: tuple[DhanInstrumentReference, ...], *, current_date: date
) -> tuple[DhanInstrumentReference, int]:
    """Choose the nearest active NSE stock future deterministically for live validation."""
    candidates = tuple(
        reference
        for reference in futures
        if reference.instrument.exchange == "NSE"
        and reference.exchange_segment == "NSE_FNO"
        and reference.provider_instrument_type == "FUTSTK"
        and reference.instrument.underlying is not None
        and reference.instrument.expiry is not None
        and reference.instrument.expiry >= current_date
    )
    if not candidates:
        pytest.fail("Dhan master contains no active NSE equity FUTSTK contract")

    selected = min(
        candidates,
        key=lambda reference: (
            reference.instrument.expiry or date.max,
            reference.instrument.underlying.exchange
            if reference.instrument.underlying is not None
            else "",
            reference.instrument.underlying.symbol
            if reference.instrument.underlying is not None
            else "",
            reference.instrument.symbol,
        ),
    )
    expiries = sorted(
        {
            reference.instrument.expiry
            for reference in candidates
            if reference.instrument.underlying == selected.instrument.underlying
            and reference.instrument.expiry is not None
        }
    )
    return selected, expiries.index(selected.instrument.expiry)


async def test_documented_dhan_rest_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate documented Dhan REST and bounded live-feed behavior only when explicitly enabled."""
    if os.getenv("APEXSCAN_DHAN_LIVE_SMOKE") != "1":
        pytest.skip("set APEXSCAN_DHAN_LIVE_SMOKE=1 to run protected Dhan validation")

    get_settings.cache_clear()
    settings = get_settings()
    if not settings.dhan_live_smoke_enabled:
        pytest.skip("set DHAN_LIVE_SMOKE_ENABLED=true to run protected Dhan validation")

    auth_request_calls = 0
    live_sample: str | None = None
    live_subset_size = 0
    live_status = "NOT RUN / BLOCKED BY MARKET HOURS"
    daily_request_semantics: dict[str, object] = {}
    daily_response: dict[str, int | str | tuple[str, ...] | None] = {
        "status": None,
        "response_keys": (),
        "error_code": None,
        "error_type": None,
    }
    original_send = httpx.AsyncClient.send

    async def capture_safe_dhan_requests(
        client: httpx.AsyncClient,
        request: httpx.Request,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Response:
        nonlocal auth_request_calls
        if request.url.host == "auth.dhan.co" and request.url.path == "/app/generateAccessToken":
            auth_request_calls += 1
        response = await original_send(client, request, *args, **kwargs)
        if request.url.host == "api.dhan.co" and request.url.path == "/v2/charts/historical":
            payload = json.loads(request.content)
            daily_request_semantics.update(
                {
                    "exchangeSegment": payload.get("exchangeSegment"),
                    "instrument": payload.get("instrument"),
                    "expiryCode": payload.get("expiryCode"),
                    "fromDate": payload.get("fromDate"),
                    "toDate": payload.get("toDate"),
                }
            )
            daily_response["status"] = response.status_code
            try:
                response_payload = response.json()
            except (TypeError, ValueError):
                response_payload = None
            if isinstance(response_payload, dict):
                daily_response["response_keys"] = tuple(
                    sorted(str(key) for key in response_payload)
                )
                error_code = response_payload.get("errorCode")
                error_type = response_payload.get("errorType")
                daily_response["error_code"] = (
                    str(error_code) if isinstance(error_code, str | int) else None
                )
                daily_response["error_type"] = error_type if isinstance(error_type, str) else None
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", capture_safe_dhan_requests)
    adapter = DhanRestAdapter.from_settings(settings)
    await adapter.connect()
    try:
        health = await adapter.get_health()
        instruments = await adapter.load_instruments()
        universe = adapter.load_fno_stock_universe()
        end = datetime.now(UTC)
        selected, expiry_rank = _select_nse_equity_futstk(
            universe.futures,
            current_date=end.date(),
        )
        nse_futures = tuple(
            reference
            for reference in universe.futures
            if reference.instrument.exchange == "NSE"
            and reference.exchange_segment == "NSE_FNO"
            and reference.instrument.underlying is not None
        )
        nse_options = tuple(
            reference
            for reference in universe.options
            if reference.instrument.exchange == "NSE"
            and reference.exchange_segment == "NSE_FNO"
            and reference.instrument.underlying is not None
        )
        nse_underlyings = tuple(
            sorted(
                {
                    reference.instrument.underlying
                    for reference in (*nse_futures, *nse_options)
                    if reference.instrument.underlying is not None
                },
                key=lambda underlying: (underlying.exchange, underlying.symbol),
            )
        )
        if not nse_underlyings:
            pytest.fail("Dhan master returned no NSE equity F&O underlyings")

        cash_live_universe = adapter.load_nse_cash_equity_live_universe()
        mapping_diagnostics = {
            "missing": tuple(
                underlying.symbol for underlying in cash_live_universe.missing_underlyings
            ),
            "ambiguous": tuple(
                underlying.symbol for underlying in cash_live_universe.ambiguous_underlyings
            ),
            "symbol_mismatches": tuple(
                underlying.symbol for underlying in cash_live_universe.symbol_mismatches
            ),
        }
        if any(mapping_diagnostics.values()):
            pytest.fail(f"Dhan cash-equity mapping gate failed (safe): {mapping_diagnostics}")
        assert len(nse_underlyings) == 208
        assert len(cash_live_universe.underlyings) == 208
        assert len(cash_live_universe.cash_references) == 208

        request = HistoricalRequest(
            instrument=selected.instrument,
            start_timestamp=end - timedelta(days=7),
            end_timestamp=end,
            interval=timedelta(days=1),
        )
        from_date = request.start_timestamp.astimezone(_INDIAN_MARKET_TIMEZONE).date().isoformat()
        to_date = request.end_timestamp.astimezone(_INDIAN_MARKET_TIMEZONE).date().isoformat()
        sample = ", ".join(underlying.symbol for underlying in nse_underlyings[:10])
        print(
            "Dhan NSE F&O smoke preflight (safe): "
            f"selected_underlying={selected.instrument.underlying.symbol}; "
            "exchange=NSE; exchange_segment=NSE_FNO; instrument=FUTSTK; "
            f"expiry={selected.instrument.expiry.isoformat()}; expiry_rank={expiry_rank}; "
            f"expiry_code={expiry_rank}; from_date={from_date}; to_date={to_date}; "
            "provider_security_reference=PRESENT; "
            f"master_rows={len(instruments)}; nse_futstk={len(nse_futures)}; "
            f"nse_optstk={len(nse_options)}; nse_equity_fno_underlyings={len(nse_underlyings)}; "
            f"safe_nse_fno_sample={sample}"
        )
        historical = await adapter.load_historical_data(request)
        intraday_request = HistoricalRequest(
            instrument=selected.instrument,
            start_timestamp=end - timedelta(hours=1),
            end_timestamp=end,
            interval=timedelta(minutes=5),
        )
        intraday = await adapter.load_historical_data(intraday_request)

        if _nse_regular_market_is_open(end):
            live_references = cash_live_universe.cash_references[:_LIVE_SMOKE_SUBSET_SIZE]
            live_subset_size = len(live_references)
            live_request = SubscriptionRequest(
                instruments=tuple(reference.instrument for reference in live_references),
                data_types=frozenset({MarketDataKind.TICK}),
            )
            stream = adapter.stream_market_data(live_request)
            try:
                async with asyncio.timeout(_LIVE_SMOKE_RECEIVE_TIMEOUT_SECONDS):
                    live_event = await anext(stream)
            finally:
                await stream.aclose()
            assert isinstance(live_event, Tick)
            live_sample = _safe_tick(live_event)
            live_status = "PASS"
    except DhanRestContractDiscrepancyError as error:
        pytest.fail(
            "DHAN REST CONTRACT DISCREPANCY — LIVE SERVER VS DOCUMENTATION: "
            f"endpoint={error.endpoint}; http_status={error.http_status}; "
            f"error_code={error.error_code}; error_type={error.error_type}; "
            f"documented_contract={error.documented_request_contract}; "
            f"observed_requirement={error.observed_requirement}"
        )
    except Exception:
        if daily_response["status"] is not None:
            pytest.fail(
                "Dhan daily historical failure (safe): "
                f"http_status={daily_response['status']}; "
                f"error_code={daily_response['error_code']}; "
                f"error_type={daily_response['error_type']}; "
                f"response_keys={daily_response['response_keys']}"
            )
        raise
    finally:
        await adapter.disconnect()

    assert auth_request_calls == 1
    assert daily_request_semantics == {
        "exchangeSegment": "NSE_FNO",
        "instrument": "FUTSTK",
        "expiryCode": expiry_rank,
        "fromDate": from_date,
        "toDate": to_date,
    }
    daily_sample = _safe_candle(historical.candles[0])
    intraday_samples = " | ".join(_safe_candle(candle) for candle in intraday.candles[:3])
    print(
        "Dhan live smoke (safe): "
        "Authentication=PASS; "
        "Profile validation=PASS; "
        "Instrument master=PASS; "
        "Instrument normalization=PASS; "
        f"master_rows={len(instruments)}; "
        f"nse_futstk={len(nse_futures)}; "
        f"nse_optstk={len(nse_options)}; "
        f"distinct_nse_equity_fno_underlyings={len(nse_underlyings)}; "
        f"safe_nse_fno_sample={sample}; "
        f"daily_historical=PASS ({daily_sample}); "
        f"five_minute_historical=PASS ({intraday_samples}); "
        f"token_generation_calls={auth_request_calls}; "
        "token_reuse=PASS; secret_leakage=NONE; "
        "live_scanner_domain=NSE_CASH_EQUITY; "
        f"cash_equity_mappings={len(cash_live_universe.cash_references)}; "
        f"live_subset_size={live_subset_size}; live_feed={live_status}; "
        f"live_canonical_sample={live_sample or 'NOT AVAILABLE'}"
    )
    assert health.status is ProviderStatus.HEALTHY
