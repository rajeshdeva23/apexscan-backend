"""Sector shadow runtime lifecycle inside LiveMarketRuntime (SECTOR-VIEW-1B).

Proves the feature flag gates the observer/worker, that exactly one observer and one evaluator
task exist when enabled, that shutdown cancels the worker and detaches the observer, and that a
shadow fault never touches ingestion.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.events.bus import EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextCreated
from app.market_intelligence.sector import MembershipResolver, load_sector_membership_dataset
from app.schemas.market_data import Instrument, ProviderSessionOhlc, Tick
from app.services.market_runtime import LiveMarketRuntime
from app.services.sector_intelligence import SectorShadowRuntime, ShadowRuntimeConfig

_NOW = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
_TD = date(2026, 9, 3)
_RESOLVER = MembershipResolver(load_sector_membership_dataset())
_IDENTITY = next(
    identity
    for sector_id in _RESOLVER.all_primary_sectors()
    for identity in _RESOLVER.members_of_primary_sector(sector_id)
)


def _settings() -> Settings:
    return Settings(
        app_env="development",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/apexscan",
        redis_url="redis://localhost:6379/0",
    )


def _shadow_factory() -> Callable[[EventBus], SectorShadowRuntime]:
    def factory(bus: EventBus) -> SectorShadowRuntime:
        return SectorShadowRuntime(
            bus=bus,
            resolver=_RESOLVER,
            config=ShadowRuntimeConfig(interval_seconds=60),
            clock=ManualClock(_NOW),
        )

    return factory


def _runtime(*, enabled: bool) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=3,
        clock=ManualClock(_NOW),
        sector_shadow_factory=_shadow_factory() if enabled else None,
    )


def _context() -> MarketContext:
    exchange, symbol = _IDENTITY.split(":")
    instrument = Instrument(exchange=exchange, symbol=symbol)
    tick = Tick(
        instrument=instrument,
        event_timestamp=_NOW - timedelta(minutes=2),
        last_price=Decimal("105"),
        traded_quantity=1,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("101"),
            high_price=Decimal("106"),
            low_price=Decimal("100"),
            close_price=Decimal("105"),
        ),
    )
    session = SessionContext(
        trading_date=_TD, market_state=MarketState.LIVE_SESSION, exchange_timezone="Asia/Kolkata"
    )
    return MarketContext.initial(
        instrument,
        sequence=1,
        event_timestamp=tick.event_timestamp,
        observed_at=_NOW,
        latest_tick=tick,
        session=session,
        previous_close=Decimal("100"),
    )


def test_flag_off_wires_no_observer_or_worker() -> None:
    runtime = _runtime(enabled=False)
    assert runtime.sector_shadow is None
    assert runtime._sector_shadow_task is None


def _shadow_handler_count(runtime: LiveMarketRuntime) -> int:
    """Count bus handlers owned by the shadow observer (other components also subscribe)."""
    shadow = runtime.sector_shadow
    observer = shadow._observer if shadow is not None else None
    count = 0
    for handlers in runtime._bus._subscribers.values():
        for handler in handlers:
            if getattr(handler, "__self__", None) is observer:
                count += 1
    return count


async def test_flag_on_starts_exactly_one_worker_and_one_observer() -> None:
    runtime = _runtime(enabled=True)
    assert runtime.sector_shadow is not None
    await runtime.start()
    try:
        assert runtime._sector_shadow_task is not None
        assert not runtime._sector_shadow_task.done()
        # One observer, subscribed to both Created and Updated => 2 shadow-owned handlers.
        assert _shadow_handler_count(runtime) == 2
    finally:
        await runtime.shutdown()


async def test_double_start_does_not_duplicate_observer_or_worker() -> None:
    runtime = _runtime(enabled=True)
    await runtime.start()
    first_task = runtime._sector_shadow_task
    await runtime.start()  # idempotent
    try:
        assert runtime._sector_shadow_task is first_task
        assert _shadow_handler_count(runtime) == 2
    finally:
        await runtime.shutdown()


async def test_shutdown_cancels_worker_and_detaches_observer() -> None:
    runtime = _runtime(enabled=True)
    await runtime.start()
    task = runtime._sector_shadow_task
    await runtime.shutdown()
    assert task is not None and task.cancelled()
    assert _shadow_handler_count(runtime) == 0  # observer detached


async def test_observer_records_published_context_and_evaluator_produces_snapshot() -> None:
    runtime = _runtime(enabled=True)
    await runtime.start()
    try:
        runtime._bus.publish(MarketContextCreated(context=_context()))
        shadow = runtime.sector_shadow
        assert shadow is not None
        snapshot = await shadow.evaluate_once()
        assert snapshot is not None
        assert snapshot.observed_count == 1
        assert snapshot.complete_count == 1
    finally:
        await runtime.shutdown()


def test_observer_callback_never_raises_on_a_broken_context() -> None:
    # A shadow fault must never propagate into the synchronous bus (ingestion).
    from app.services.sector_intelligence.diagnostics import ShadowDiagnostics
    from app.services.sector_intelligence.observer import SectorShadowObserver
    from app.services.sector_intelligence.state import ObservationState

    diagnostics = ShadowDiagnostics()
    observer = SectorShadowObserver(
        bus=EventBus(), state=ObservationState(frozenset()), diagnostics=diagnostics
    )

    class _Broken:
        @property
        def context(self) -> MarketContext:
            raise ValueError("boom")

    observer._on_context(_Broken())  # type: ignore[arg-type]  # must not raise
    assert diagnostics.events_rejected == 1
