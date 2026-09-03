"""Coarse performance characterization of the sector shadow runtime (SECTOR-VIEW-1B).

Measures the O(1) bus-callback cost and one full whole-universe evaluation. These are
observability numbers, NOT a production SLO; the assertions are deliberately loose so the test
is not machine-flaky. The 210 figure is derived from the real universe, never hardcoded.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.events.bus import EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextCreated
from app.market_intelligence.sector import MembershipResolver, load_sector_membership_dataset
from app.schemas.market_data import Instrument, ProviderSessionOhlc, Tick
from app.services.sector_intelligence import SectorShadowRuntime, ShadowRuntimeConfig

_TD = date(2026, 9, 3)
_EVAL = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
_TS = datetime(2026, 9, 3, 6, 58, tzinfo=UTC)
_RESOLVER = MembershipResolver(load_sector_membership_dataset())
_UNIVERSE = tuple(
    identity
    for sector_id in _RESOLVER.all_primary_sectors()
    for identity in _RESOLVER.members_of_primary_sector(sector_id)
)


def _context(identity: str, version: int) -> MarketContext:
    exchange, symbol = identity.split(":")
    instrument = Instrument(exchange=exchange, symbol=symbol)
    tick = Tick(
        instrument=instrument,
        event_timestamp=_TS,
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
        sequence=version,
        event_timestamp=_TS,
        observed_at=_TS,
        latest_tick=tick,
        session=session,
        previous_close=Decimal("100"),
    )


def _percentiles(samples_ms: list[float]) -> tuple[float, float, float]:
    ordered = sorted(samples_ms)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return statistics.median(ordered), p95, ordered[-1]


def test_callback_and_evaluation_performance(capsys: pytest.CaptureFixture[str]) -> None:
    bus = EventBus()
    runtime = SectorShadowRuntime(
        bus=bus,
        resolver=_RESOLVER,
        config=ShadowRuntimeConfig(interval_seconds=60),
        clock=ManualClock(_EVAL),
    )
    runtime.subscribe()

    callback_ms: list[float] = []
    for version, identity in enumerate(_UNIVERSE, start=1):
        event = MarketContextCreated(context=_context(identity, version))
        start = time.perf_counter()
        bus.publish(event)
        callback_ms.append((time.perf_counter() - start) * 1000.0)

    eval_ms: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        asyncio.run(runtime.evaluate_once())
        eval_ms.append((time.perf_counter() - start) * 1000.0)

    cb_median, cb_p95, cb_max = _percentiles(callback_ms)
    ev_median, ev_p95, ev_max = _percentiles(eval_ms)
    with capsys.disabled():
        print(
            f"\n[shadow-perf] universe={len(_UNIVERSE)} "
            f"callback_ms median={cb_median:.4f} p95={cb_p95:.4f} max={cb_max:.4f} | "
            f"eval_ms median={ev_median:.3f} p95={ev_p95:.3f} max={ev_max:.3f}"
        )

    assert cb_max < 50.0  # O(1) callback stays well under a tick budget
    assert ev_median < 1000.0  # whole-universe shadow evaluation is sub-second
