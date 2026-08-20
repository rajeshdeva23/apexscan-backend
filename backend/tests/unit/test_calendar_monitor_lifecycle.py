"""Calendar-monitor driver scheduling and runtime task ownership (ADR-011; ADR-010).

Drives the once-per-day scheduler with a fake clock and fake sleep (no wall clock, no
network) and proves the broker-neutral runtime owns exactly one extra managed task,
cancels it on shutdown, records a fatal end, and leaves ingestion/refresh ownership intact.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, time, timedelta

import httpx

from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.market_engine.clock import ManualClock
from app.schemas.market_data import (
    Instrument,
    MarketData,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
)
from app.services.calendar_monitor import CalendarMonitorDriver, CalendarMonitorState
from app.services.dhan_runtime_composition import LiveMarketRuntimeDependency
from app.services.market_runtime import LiveMarketRuntime

_NOW = datetime(2026, 8, 16, 6, 30, tzinfo=UTC)  # 12:00 IST — past the 08:00 run time
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_HTML = (
    "<html><body><table>"
    "<tr><th>Date</th><th>Segment</th><th>Status</th></tr>"
    "<tr><td>2026-01-26</td><td>NSE Equity</td><td>Closed</td></tr>"
    "</table></body></html>"
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "development", "database_url": _DB, "redis_url": _REDIS}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _RecordingMonitor:
    """A neutral monitor double recording each scheduled check reference."""

    def __init__(self) -> None:
        self.references: list[datetime] = []

    async def check(self, *, reference: datetime) -> CalendarMonitorState | None:
        self.references.append(reference)
        return None


class _ExplodingMonitor:
    """A monitor double whose check raises to exercise the fatal-end path."""

    async def check(self, *, reference: datetime) -> CalendarMonitorState | None:
        raise RuntimeError("monitor boom")


class _ClockSleep:
    """A fake sleep that advances the injected clock and cancels after a bounded count."""

    def __init__(self, clock: ManualClock, *, step: timedelta, max_calls: int) -> None:
        self._clock = clock
        self._step = step
        self._max_calls = max_calls
        self._calls = 0

    async def __call__(self, _seconds: float) -> None:
        self._calls += 1
        if self._calls > self._max_calls:
            raise asyncio.CancelledError
        self._clock.advance(self._step)


async def _drain(driver: CalendarMonitorDriver) -> None:
    task = asyncio.create_task(driver.run())
    try:
        await task
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Driver scheduling (fake clock + fake sleep)
# --------------------------------------------------------------------------- #
async def test_driver_fires_once_per_day_and_again_next_day() -> None:
    clock = ManualClock(datetime(2026, 8, 16, 0, 0, tzinfo=UTC))  # 05:30 IST
    monitor = _RecordingMonitor()
    driver = CalendarMonitorDriver(
        monitor=monitor,  # type: ignore[arg-type]
        clock=clock,
        exchange_timezone="Asia/Kolkata",
        run_time=time(8, 0),
        poll_seconds=1.0,
        sleep=_ClockSleep(clock, step=timedelta(hours=1), max_calls=30),
    )
    await _drain(driver)
    assert len(monitor.references) == 2  # exactly one fire per calendar day
    assert monitor.references[1] - monitor.references[0] == timedelta(days=1)


async def test_driver_fires_once_at_startup_after_run_time() -> None:
    clock = ManualClock(datetime(2026, 8, 16, 3, 30, tzinfo=UTC))  # 09:00 IST, already past 08:00
    monitor = _RecordingMonitor()
    driver = CalendarMonitorDriver(
        monitor=monitor,  # type: ignore[arg-type]
        clock=clock,
        exchange_timezone="Asia/Kolkata",
        run_time=time(8, 0),
        poll_seconds=1.0,
        sleep=_ClockSleep(clock, step=timedelta(minutes=30), max_calls=6),
    )
    await _drain(driver)
    assert len(monitor.references) == 1  # fires once that day, not repeatedly


# --------------------------------------------------------------------------- #
# Runtime ownership (broker-neutral LiveMarketRuntime)
# --------------------------------------------------------------------------- #
def _runtime_with_monitor(monitor: object) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=3,
        calendar_monitor=monitor,  # type: ignore[arg-type]
        calendar_monitor_run_time=time(8, 0),
        clock=ManualClock(_NOW),
    )


async def test_enabled_monitor_runs_one_task_and_shutdown_cancels_it() -> None:
    runtime = _runtime_with_monitor(_RecordingMonitor())
    await runtime.start()
    status = runtime.status()
    assert status.calendar_monitor_configured is True
    assert status.calendar_monitor_running is True
    await runtime.shutdown()
    assert runtime.status().calendar_monitor_running is False


async def test_disabled_monitor_runs_no_task() -> None:
    runtime = LiveMarketRuntime(settings=_settings(), error_threshold=3, clock=ManualClock(_NOW))
    await runtime.start()
    status = runtime.status()
    assert status.calendar_monitor_configured is False
    assert status.calendar_monitor_running is False
    await runtime.shutdown()


async def test_monitor_task_failure_is_observed() -> None:
    runtime = _runtime_with_monitor(_ExplodingMonitor())
    await runtime.start()
    for _ in range(200):
        if runtime.status().fatal_calendar_monitor_error:
            break
        await asyncio.sleep(0)
    status = runtime.status()
    assert status.fatal_calendar_monitor_error is True
    assert status.calendar_monitor_running is False
    await runtime.shutdown()  # no orphan; safe after the task already ended


# --------------------------------------------------------------------------- #
# Composition: ingestion + refresh + monitor all own a task (three managed tasks)
# --------------------------------------------------------------------------- #
class _Provider:
    """A provider double: universe + lifecycle + session-stat source + blocking stream."""

    capabilities = frozenset()

    def __init__(self) -> None:
        self._gate = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        return (Instrument(exchange="NSE", symbol="RELIANCE"),)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        return DhanCashEquityLiveUniverse(
            underlyings=(),
            cash_references=(
                DhanInstrumentReference(
                    instrument=Instrument(exchange="NSE", symbol="RELIANCE"),
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
        self, instruments: Sequence[Instrument], *, trading_date: object, observed_at: object
    ) -> tuple[()]:
        return ()

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for _ in ():
            yield _  # pragma: no cover
        await self._gate.wait()


def _enabled_monitor_settings() -> Settings:
    return _settings(
        market_provider_enabled=True,
        calendar_monitor_enabled=True,
        dhan_client_id="c",
        dhan_pin="123456",
        dhan_totp_secret="s",
    )


async def test_enabled_composition_owns_three_managed_tasks() -> None:
    handler = lambda request: httpx.Response(200, text=_HTML)  # noqa: E731
    dependency = LiveMarketRuntimeDependency(
        settings=_enabled_monitor_settings(),
        error_threshold=3,
        adapter=_Provider(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
        clock=ManualClock(_NOW),
    )
    await dependency.start(5.0)
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    status = composition.runtime.status()
    assert status.ingestion_running is True
    assert status.refresh_driver_running is True
    assert status.calendar_monitor_running is True  # ingestion + refresh + monitor
    await dependency.shutdown()
    assert composition.runtime.status().calendar_monitor_running is False


async def test_disabled_monitor_composition_owns_no_monitor_task() -> None:
    dependency = LiveMarketRuntimeDependency(
        settings=_settings(
            market_provider_enabled=True,
            dhan_client_id="c",
            dhan_pin="123456",
            dhan_totp_secret="s",
        ),
        error_threshold=3,
        adapter=_Provider(),  # type: ignore[arg-type]
        clock=ManualClock(_NOW),
    )
    await dependency.start(5.0)
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    status = composition.runtime.status()
    assert status.calendar_monitor_configured is False
    assert status.ingestion_running is True  # existing ownership unchanged
    assert status.refresh_driver_running is True
    await dependency.shutdown()
