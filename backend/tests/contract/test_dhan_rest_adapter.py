"""Reusable P3.1 contract coverage for the applicable Dhan REST capabilities."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.adapters.dhan import DhanRestAdapter
from app.schemas.market_data import HistoricalRequest, Instrument, InstrumentClass, MarketSegment
from tests.contract import provider_adapter_contract

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


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _json(name: str) -> dict[str, object]:
    return json.loads(_text(name))


async def test_dhan_rest_adapter_satisfies_shared_historical_and_instrument_contracts() -> None:
    """Changing a canonical Dhan output into a provider DTO would fail this contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "images.dhan.co":
            return httpx.Response(200, text=_text("instrument_master_detailed.csv"))
        if request.url.path == "/v2/profile":
            return httpx.Response(200, json={"tokenValidity": "fixture-only"})
        return httpx.Response(200, json=_json("daily_historical.json"))

    adapter = DhanRestAdapter(
        access_token=SecretStr("fixture-only-token"),
        transport=httpx.MockTransport(handler),
    )
    request = HistoricalRequest(
        instrument=_CASH_INSTRUMENT,
        start_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        end_timestamp=datetime(2025, 1, 3, tzinfo=UTC),
        interval=timedelta(days=1),
    )

    (
        health,
        instruments,
        historical,
    ) = await provider_adapter_contract.assert_historical_instrument_adapter_contract(
        adapter, request
    )

    assert health.status.value == "healthy"
    assert all(isinstance(instrument, Instrument) for instrument in instruments)
    assert all(candle.instrument == request.instrument for candle in historical.candles)
