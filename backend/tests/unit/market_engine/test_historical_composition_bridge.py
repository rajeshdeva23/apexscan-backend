"""Production HistoricalSource bridge and Market Engine isolation (P4.5E; §13,30,33)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.base import ProviderRateLimitError
from app.market_engine.historical.source import HistoricalSource, HistoricalSourceError
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, HistoricalRequest, HistoricalResult, Instrument
from app.services.historical_source_bridge import (
    DHAN_DIRECT_TIMEFRAMES,
    BrokerHistoricalSource,
    broker_historical_source,
)
from tests.architecture.import_boundary import scan_market_engine

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")


class _FakeAdapter:
    """A minimal HistoricalDataAdapter: returns one candle or raises a provider error."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        self.calls += 1
        if self._fail:
            raise ProviderRateLimitError
        candle = Candle(
            instrument=request.instrument,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.start_timestamp + request.interval,
            open_price=Decimal("100"),
            high_price=Decimal("101"),
            low_price=Decimal("99"),
            close_price=Decimal("100"),
            traded_quantity=10,
        )
        return HistoricalResult(request=request, candles=(candle,))


def _request() -> HistoricalRequest:
    start = datetime(2026, 8, 6, 3, 45, tzinfo=UTC)
    return HistoricalRequest(
        instrument=_INSTRUMENT,
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=5),
        interval=timedelta(minutes=5),
    )


def test_bridge_satisfies_historical_source_protocol() -> None:
    bridge = BrokerHistoricalSource(
        adapter=_FakeAdapter(), direct_timeframes=DHAN_DIRECT_TIMEFRAMES
    )
    assert isinstance(bridge, HistoricalSource)


def test_bridge_advertises_configured_direct_timeframes() -> None:
    bridge = broker_historical_source(_FakeAdapter())
    assert Timeframe.minutes(5) in bridge.direct_timeframes
    assert Timeframe.session() in bridge.direct_timeframes
    assert Timeframe.minutes(7) not in bridge.direct_timeframes


async def test_bridge_delegates_load() -> None:
    adapter = _FakeAdapter()
    bridge = broker_historical_source(adapter)
    result = await bridge.load(_request())
    assert adapter.calls == 1
    assert result.candles[0].instrument == _INSTRUMENT


async def test_bridge_translates_provider_error() -> None:
    bridge = broker_historical_source(_FakeAdapter(fail=True))
    with pytest.raises(HistoricalSourceError):
        await bridge.load(_request())


def test_market_engine_still_imports_no_concrete_adapter() -> None:
    app_root = Path(__file__).parents[2] / "app"
    assert scan_market_engine(app_root) == {}


def test_bridge_lives_outside_market_engine() -> None:
    from app.services import historical_source_bridge

    assert "market_engine" not in historical_source_bridge.__name__
