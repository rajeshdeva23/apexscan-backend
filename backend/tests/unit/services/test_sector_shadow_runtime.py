"""Periodic evaluator behavior for the passive sector shadow runtime (SECTOR-VIEW-1B).

Uses the real SECTOR-2 membership dataset (effective 2026-09-02) so the trading date must be on
or after it. Contexts are crafted directly to isolate the observer/evaluator; VIEW-1A already
covers the MarketContext carry-forward that keeps previous_close populated across ticks.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
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
_NEXT = date(2026, 9, 4)
_EVAL = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
_FRESH_TS = datetime(2026, 9, 3, 6, 58, tzinfo=UTC)  # 2 min old at _EVAL (fresh <= 5 min)

_RESOLVER = MembershipResolver(load_sector_membership_dataset())
_UNIVERSE = tuple(
    identity
    for sector_id in _RESOLVER.all_primary_sectors()
    for identity in _RESOLVER.members_of_primary_sector(sector_id)
)


def _sector_sizes() -> dict[str, int]:
    return {
        sector_id: len(_RESOLVER.members_of_primary_sector(sector_id, on=_TD))
        for sector_id in _RESOLVER.all_primary_sectors()
    }


def _context(
    identity: str,
    *,
    ts: datetime = _FRESH_TS,
    version: int = 1,
    last: Decimal | None = Decimal("105"),
    prev: Decimal | None = Decimal("100"),
    session_open: Decimal | None = Decimal("101"),
    td: date | None = _TD,
) -> MarketContext:
    exchange, symbol = identity.split(":")
    instrument = Instrument(exchange=exchange, symbol=symbol)
    tick = None
    if last is not None:
        ohlc = (
            ProviderSessionOhlc(
                open_price=session_open,
                high_price=max(session_open, last) + Decimal("1"),
                low_price=min(session_open, last) - Decimal("1"),
                close_price=last,
            )
            if session_open is not None
            else None
        )
        tick = Tick(
            instrument=instrument,
            event_timestamp=ts,
            last_price=last,
            traded_quantity=1,
            session_ohlc=ohlc,
        )
    session = (
        SessionContext(
            trading_date=td, market_state=MarketState.LIVE_SESSION, exchange_timezone="Asia/Kolkata"
        )
        if td is not None
        else None
    )
    return MarketContext.initial(
        instrument,
        sequence=version,
        event_timestamp=ts,
        observed_at=ts,
        latest_tick=tick,
        session=session,
        previous_close=prev,
    )


def _runtime(
    *,
    clock: ManualClock | None = None,
    hook: Callable[[], Awaitable[None]] | None = None,
) -> tuple[SectorShadowRuntime, EventBus]:
    bus = EventBus()
    runtime = SectorShadowRuntime(
        bus=bus,
        resolver=_RESOLVER,
        config=ShadowRuntimeConfig(interval_seconds=60),
        clock=clock or ManualClock(_EVAL),
        evaluation_hook=hook,
    )
    runtime.subscribe()
    return runtime, bus


def _publish(bus: EventBus, context: MarketContext) -> None:
    bus.publish(MarketContextCreated(context=context))


def test_config_rejects_out_of_range_interval() -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        ShadowRuntimeConfig(interval_seconds=0)
    with pytest.raises(ValueError, match="interval_seconds"):
        ShadowRuntimeConfig(interval_seconds=3601)


def test_snapshot_is_created_with_expected_universe_and_complete_counts() -> None:
    runtime, bus = _runtime()
    for identity in _UNIVERSE[:5]:
        _publish(bus, _context(identity))
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    assert snapshot.expected_universe_count == len(_UNIVERSE)
    assert snapshot.observed_count == 5
    assert snapshot.complete_count == 5
    assert snapshot.fresh_count == 5
    assert snapshot.trading_date == _TD
    assert snapshot.universe_proxy is not None
    assert len(snapshot.sector_metrics) == len(_RESOLVER.all_primary_sectors())


def test_all_previous_close_missing_stays_healthy_with_zero_complete() -> None:
    runtime, bus = _runtime()
    for identity in _UNIVERSE[:10]:
        _publish(bus, _context(identity, prev=None))
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    assert snapshot.observed_count == 10
    assert snapshot.complete_count == 0
    assert snapshot.missing_previous_close_count == 10
    assert snapshot.fresh_count == 0
    # No fake returns or direction: every sector is insufficient / no median.
    assert all(m.median_intraday_return is None for m in snapshot.sector_metrics)
    assert all(not r.ranked_stocks for r in snapshot.stock_rankings)
    assert runtime.diagnostics().snapshot_successes == 1


def test_partial_completeness_only_complete_fresh_instruments_participate() -> None:
    runtime, bus = _runtime()
    complete = _UNIVERSE[:4]
    for identity in complete:
        _publish(bus, _context(identity))
    _publish(bus, _context(_UNIVERSE[4], prev=None))  # missing previous_close
    _publish(bus, _context(_UNIVERSE[5], session_open=None))  # missing session_open
    _publish(bus, _context(_UNIVERSE[6], last=None))  # missing last_price (no tick)
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    assert snapshot.observed_count == 7
    assert snapshot.complete_count == 4
    assert snapshot.missing_previous_close_count == 1  # only the prev=None context
    assert snapshot.missing_session_open_count == 2  # the session_open=None and last=None context
    assert snapshot.missing_last_price_count == 1


def test_missing_session_open_excludes_the_instrument() -> None:
    runtime, bus = _runtime()
    _publish(bus, _context(_UNIVERSE[0]))
    _publish(bus, _context(_UNIVERSE[1], session_open=None))
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    assert snapshot.complete_count == 1
    assert snapshot.missing_session_open_count == 1


def test_late_completion_makes_an_instrument_eligible_without_restart() -> None:
    runtime, bus = _runtime()
    identity = _UNIVERSE[0]
    _publish(bus, _context(identity, prev=None, version=1))
    first = asyncio.run(runtime.evaluate_once())
    assert first is not None and first.complete_count == 0
    # A later context carries a valid previous_close (as VIEW-1A carry-forward would).
    _publish(
        bus, _context(identity, prev=Decimal("100"), version=2, ts=_FRESH_TS + timedelta(seconds=1))
    )
    second = asyncio.run(runtime.evaluate_once())
    assert second is not None and second.complete_count == 1


def test_fresh_to_stale_transition_lowers_coverage() -> None:
    clock = ManualClock(_EVAL)
    runtime, bus = _runtime(clock=clock)
    for identity in _UNIVERSE[:3]:
        _publish(bus, _context(identity))
    fresh = asyncio.run(runtime.evaluate_once())
    assert fresh is not None and fresh.fresh_count == 3
    clock.set(_EVAL + timedelta(minutes=30))  # now well beyond the 5-min freshness limit
    stale = asyncio.run(runtime.evaluate_once())
    assert stale is not None
    assert stale.fresh_count == 0
    assert stale.stale_count == 3
    assert stale.universe_proxy is not None and stale.universe_proxy.valid_count == 0


def test_future_timestamp_is_not_fresh() -> None:
    runtime, bus = _runtime()
    _publish(bus, _context(_UNIVERSE[0], ts=_EVAL + timedelta(minutes=1)))  # future
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    assert snapshot.complete_count == 1
    assert snapshot.fresh_count == 0  # age < 0 fails closed


def test_trading_date_rollover_excludes_prior_day_from_evaluation() -> None:
    clock = ManualClock(_EVAL)
    runtime, bus = _runtime(clock=clock)
    for identity in _UNIVERSE[:3]:
        _publish(bus, _context(identity, td=_TD))
    day1 = asyncio.run(runtime.evaluate_once())
    assert day1 is not None and day1.complete_count == 3

    clock.set(datetime(2026, 9, 4, 7, 0, tzinfo=UTC))
    _publish(bus, _context(_UNIVERSE[0], td=_NEXT, ts=datetime(2026, 9, 4, 6, 58, tzinfo=UTC)))
    day2 = asyncio.run(runtime.evaluate_once())
    assert day2 is not None
    assert day2.trading_date == _NEXT
    assert day2.observed_count == 1  # prior-day observations cleared
    assert day2.complete_count == 1


def test_late_prior_day_event_is_rejected_after_rollover() -> None:
    clock = ManualClock(_EVAL)
    runtime, bus = _runtime(clock=clock)
    _publish(bus, _context(_UNIVERSE[0], td=_TD))
    clock.set(datetime(2026, 9, 4, 7, 0, tzinfo=UTC))
    _publish(bus, _context(_UNIVERSE[1], td=_NEXT, ts=datetime(2026, 9, 4, 6, 58, tzinfo=UTC)))
    _publish(bus, _context(_UNIVERSE[2], td=_TD, ts=_FRESH_TS))  # late prior day
    asyncio.run(runtime.evaluate_once())
    assert runtime.diagnostics().late_trading_date_events == 1


def test_unknown_instrument_is_counted_and_never_evaluated() -> None:
    runtime, bus = _runtime()
    _publish(bus, _context("NSE:NOT-IN-UNIVERSE"))
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None and snapshot.observed_count == 0
    assert runtime.diagnostics().unknown_instruments == 1


def test_determinism_and_final_state_order_invariance() -> None:
    identities = _UNIVERSE[:12]
    runtime_a, bus_a = _runtime()
    for identity in identities:
        _publish(bus_a, _context(identity))
    snap_a = asyncio.run(runtime_a.evaluate_once())

    runtime_b, bus_b = _runtime()
    for identity in reversed(identities):
        _publish(bus_b, _context(identity))
    snap_b = asyncio.run(runtime_b.evaluate_once())

    assert snap_a is not None and snap_b is not None
    assert snap_a.universe_proxy == snap_b.universe_proxy
    assert snap_a.sector_metrics == snap_b.sector_metrics
    assert snap_a.stock_rankings == snap_b.stock_rankings


def test_smallest_sector_evaluates_without_error() -> None:
    smallest = min(_sector_sizes(), key=lambda s: _sector_sizes()[s])
    members = _RESOLVER.members_of_primary_sector(smallest, on=_TD)
    runtime, bus = _runtime()
    for identity in members:
        _publish(bus, _context(identity))
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    metrics = next(m for m in snapshot.sector_metrics if m.sector_id == smallest)
    assert metrics.valid_count == len(members)


def test_largest_sector_is_equal_weighted() -> None:
    largest = max(_sector_sizes(), key=lambda s: _sector_sizes()[s])
    members = _RESOLVER.members_of_primary_sector(largest, on=_TD)
    runtime, bus = _runtime()
    # Distinct last prices → distinct intraday returns; equal-weight median must be the median.
    returns = []
    for i, identity in enumerate(members):
        last = Decimal("100") + Decimal(i)
        _publish(
            bus, _context(identity, last=last, session_open=Decimal("100"), prev=Decimal("100"))
        )
        returns.append((last - Decimal("100")) / Decimal("100"))
    snapshot = asyncio.run(runtime.evaluate_once())
    assert snapshot is not None
    metrics = next(m for m in snapshot.sector_metrics if m.sector_id == largest)
    assert metrics.valid_count == len(members)
    assert metrics.median_intraday_return == statistics.median(returns)  # equal weight, no cap tilt


def test_evaluation_failure_preserves_last_good_snapshot() -> None:
    good_runtime, bus = _runtime()
    _publish(bus, _context(_UNIVERSE[0]))
    good = asyncio.run(good_runtime.evaluate_once())
    assert good is not None

    async def _boom() -> None:
        raise RuntimeError("injected evaluation failure")

    good_runtime._evaluation_hook = _boom  # inject a failure into the next evaluation
    result = asyncio.run(good_runtime.evaluate_once())
    assert result is good  # last-good snapshot preserved, not replaced by a partial
    assert good_runtime.diagnostics().snapshot_failures == 1
    assert good_runtime.latest_snapshot() is good


def test_concurrent_evaluation_does_not_overlap() -> None:
    release = asyncio.Event()

    async def _hold() -> None:
        await release.wait()

    async def _drive() -> None:
        runtime, bus = _runtime(hook=_hold)
        _publish(bus, _context(_UNIVERSE[0]))
        first = asyncio.create_task(runtime.evaluate_once())
        await asyncio.sleep(0)  # let the first evaluation enter and block on the hook
        await runtime.evaluate_once()  # concurrent entry: must not start a calculation
        assert runtime.diagnostics().evaluation_overruns == 1
        release.set()
        await first
        assert runtime.diagnostics().snapshot_successes == 1  # only the first computed

    asyncio.run(_drive())
