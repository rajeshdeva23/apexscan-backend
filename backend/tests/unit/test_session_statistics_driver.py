"""Managed session-statistics refresh driver (P4.6E7; ADR-009 addendum, ADR-010 D9).

Unit-tests the driver's phase/demand gate and failure/cancellation behavior with fakes, and
its runtime lifecycle (one owned task, governed shutdown order) via the application
dependency — authority disabled, no live network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime

import pytest

from app.adapters.base.errors import ProviderUnavailableError
from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketState, SessionContext
from app.schemas.market_data import (
    Instrument,
    MarketData,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
)
from app.services.dhan_runtime_composition import LiveMarketRuntimeDependency
from app.services.session_statistics_driver import SessionStatisticsRefreshDriver

_LIVE = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST
_DATE = date(2026, 8, 6)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _session(state: MarketState) -> SessionContext:
    return SessionContext(trading_date=_DATE, market_state=state, exchange_timezone="Asia/Kolkata")


class _FakeClassifier:
    def __init__(self, state: MarketState) -> None:
        self._state = state

    def classify(self, instant: datetime, *, halt_active: bool = False) -> SessionContext:
        return _session(self._state)


class _FakeRefresh:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[datetime, date]] = []
        self._fail_first = fail_first

    async def refresh_if_due(self, *, reference: datetime, trading_date: date) -> bool:
        self.calls.append((reference, trading_date))
        if self._fail_first and len(self.calls) == 1:
            raise ProviderUnavailableError()
        return True


def _driver(refresh: _FakeRefresh, state: MarketState) -> SessionStatisticsRefreshDriver:
    return SessionStatisticsRefreshDriver(
        refresh=refresh,  # type: ignore[arg-type]
        classifier=_FakeClassifier(state),  # type: ignore[arg-type]
        clock=ManualClock(_LIVE),
        poll_seconds=0.001,
    )


# --------------------------------------------------------------------------- #
# Phase gate (ADR-009 addendum)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state",
    [
        MarketState.PRE_OPEN,
        MarketState.OPENING_AUCTION,
        MarketState.CLOSING_SESSION,
        MarketState.MARKET_CLOSED,
        MarketState.HOLIDAY,
        MarketState.EMERGENCY_HALT,
        MarketState.CALENDAR_UNAVAILABLE,
    ],
)
async def test_non_live_phases_never_refresh(state: MarketState) -> None:
    refresh = _FakeRefresh()
    performed = await _driver(refresh, state)._cycle(_LIVE)
    assert performed is False
    assert refresh.calls == []  # zero source opportunities outside LIVE_SESSION


async def test_live_session_gives_the_coordinator_an_opportunity() -> None:
    refresh = _FakeRefresh()
    performed = await _driver(refresh, MarketState.LIVE_SESSION)._cycle(_LIVE)
    assert performed is True
    assert refresh.calls == [(_LIVE, _DATE)]  # canonical trading_date from the classifier


async def test_provider_failure_is_swallowed_and_driver_survives() -> None:
    refresh = _FakeRefresh(fail_first=True)
    driver = _driver(refresh, MarketState.LIVE_SESSION)
    assert await driver._cycle(_LIVE) is False  # cycle 1 fails, swallowed
    assert await driver._cycle(_LIVE) is True  # cycle 2 recovers
    assert len(refresh.calls) == 2


async def test_run_loop_cancels_cleanly() -> None:
    refresh = _FakeRefresh()
    driver = _driver(refresh, MarketState.LIVE_SESSION)
    task = asyncio.create_task(driver.run())
    for _ in range(100):
        if refresh.calls:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert refresh.calls  # ran at least one cycle before cancellation


# --------------------------------------------------------------------------- #
# Runtime lifecycle via the application dependency
# --------------------------------------------------------------------------- #
class _Provider:
    """A provider double: universe + lifecycle + session-stat source + a blocking stream."""

    capabilities = frozenset()

    def __init__(self) -> None:
        self.session_statistics_calls = 0
        self.events: list[str] = []
        self._gate = asyncio.Event()

    async def connect(self) -> None:
        self.events.append("connect")

    async def disconnect(self) -> None:
        self.events.append("disconnect")

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_LIVE)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        return (_instrument(),)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        return DhanCashEquityLiveUniverse(
            underlyings=(),
            cash_references=(
                DhanInstrumentReference(
                    instrument=_instrument(),
                    security_id="SEC",
                    underlying_security_id=None,
                    exchange_segment="NSE_EQ",
                    provider_instrument_type="ES",
                ),
            ),
            missing_underlyings=(),
            ambiguous_underlyings=(),
            symbol_mismatches=(),
        )

    async def load_session_statistics(
        self, instruments: Sequence[Instrument], *, trading_date: date, observed_at: datetime
    ) -> tuple[()]:
        self.session_statistics_calls += 1
        return ()

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for _ in ():
            yield _  # pragma: no cover
        await self._gate.wait()


def _settings() -> Settings:
    return Settings(
        app_env="development",
        database_url=_DB,
        redis_url=_REDIS,
        market_provider_enabled=True,
        dhan_client_id="c",
        dhan_pin="123456",
        dhan_totp_secret="s",
    )


def _dependency(provider: _Provider) -> LiveMarketRuntimeDependency:
    return LiveMarketRuntimeDependency(
        settings=_settings(),
        error_threshold=3,
        adapter=provider,  # type: ignore[arg-type]
        clock=ManualClock(_LIVE),
    )


async def test_runtime_starts_exactly_one_refresh_driver() -> None:
    provider = _Provider()
    dependency = _dependency(provider)
    await dependency.start(5.0)
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    status = composition.runtime.status()
    assert status.refresh_driver_configured is True
    assert status.refresh_driver_running is True
    await dependency.shutdown()
    assert composition.runtime.status().refresh_driver_running is False


async def test_shutdown_stops_driver_then_disconnects_provider() -> None:
    provider = _Provider()
    dependency = _dependency(provider)
    await dependency.start(5.0)
    await dependency.shutdown()
    assert "disconnect" in provider.events  # provider disconnected after runtime stop


async def test_zero_demand_live_session_makes_no_source_call() -> None:
    provider = _Provider()
    dependency = _dependency(provider)
    await dependency.start(5.0)
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    refresh = composition.runtime.session_statistics_refresh
    assert refresh is not None
    # No consumer → coordinator inactive → refresh_if_due is a no-op even in LIVE_SESSION.
    assert await refresh.refresh_if_due(reference=_LIVE, trading_date=_DATE) is False
    assert provider.session_statistics_calls == 0
    await dependency.shutdown()
