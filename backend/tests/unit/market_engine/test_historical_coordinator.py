"""Cache, coalescing, bounded concurrency, and failure sharing (P4.5B; §42)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.source import (
    HistoricalFetchPlan,
    HistoricalSourceError,
    interval_for_timeframe,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from tests.fakes.historical_source import Behavior, FakeHistoricalSource

_FIVE = Timeframe.minutes(5)
_START = datetime(2026, 8, 7, 3, 45, tzinfo=UTC)


def _plan(
    symbol: str = "RELIANCE", *, offset_min: int = 0, minutes: int = 120
) -> HistoricalFetchPlan:
    start = _START + timedelta(minutes=offset_min)
    return HistoricalFetchPlan(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        requirement=HistoricalRequirement(timeframe=_FIVE, lookback=1),
        start=start,
        end=start + timedelta(minutes=minutes),
        interval=interval_for_timeframe(_FIVE),
    )


def _coordinator(
    source: FakeHistoricalSource, *, max_concurrency: int = 4
) -> HistoricalCoordinator:
    return HistoricalCoordinator(
        source=source, cache=HistoricalCache(), max_concurrency=max_concurrency
    )


def test_non_positive_concurrency_is_rejected() -> None:
    source = FakeHistoricalSource(direct_timeframes=frozenset({_FIVE}))
    with pytest.raises(ValueError, match="positive integer"):
        _coordinator(source, max_concurrency=0)


async def test_identical_requests_coalesce_then_cache() -> None:
    source = FakeHistoricalSource(direct_timeframes=frozenset({_FIVE}), default=Behavior.BLOCK)
    coordinator = _coordinator(source)
    plan = _plan()
    first = asyncio.create_task(coordinator.fetch(plan))
    second = asyncio.create_task(coordinator.fetch(plan))
    await source.wait_until_active(1)
    source.release_all()
    result_a, result_b = await asyncio.gather(first, second)
    assert source.call_count == 1
    assert result_a == result_b
    assert await coordinator.fetch(plan) == result_a  # cache hit, no new call
    assert source.call_count == 1
    assert coordinator.inflight_count == 0


async def test_distinct_ranges_are_separate_calls() -> None:
    source = FakeHistoricalSource(direct_timeframes=frozenset({_FIVE}))
    coordinator = _coordinator(source)
    await coordinator.fetch(_plan(offset_min=0, minutes=60))
    await coordinator.fetch(_plan(offset_min=120, minutes=60))  # disjoint, not contained
    assert source.call_count == 2


async def test_distinct_instruments_are_separate_calls() -> None:
    source = FakeHistoricalSource(direct_timeframes=frozenset({_FIVE}))
    coordinator = _coordinator(source)
    await coordinator.fetch(_plan("RELIANCE"))
    await coordinator.fetch(_plan("TCS"))
    assert source.call_count == 2


async def test_concurrency_is_bounded() -> None:
    source = FakeHistoricalSource(direct_timeframes=frozenset({_FIVE}), default=Behavior.BLOCK)
    coordinator = _coordinator(source, max_concurrency=2)
    tasks = [asyncio.create_task(coordinator.fetch(_plan(f"SYM{index}"))) for index in range(5)]
    await source.wait_until_active(2)
    assert source.max_active == 2
    source.release_all()
    await asyncio.gather(*tasks)
    assert source.max_active == 2
    assert coordinator.inflight_count == 0


async def test_failure_is_shared_and_retryable() -> None:
    source = FakeHistoricalSource(
        direct_timeframes=frozenset({_FIVE}), default=Behavior.BLOCK_THEN_FAIL
    )
    coordinator = _coordinator(source)
    plan = _plan()
    first = asyncio.create_task(coordinator.fetch(plan))
    second = asyncio.create_task(coordinator.fetch(plan))
    await source.wait_until_active(1)
    source.release_all()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, HistoricalSourceError) for result in results)
    assert source.call_count == 1
    assert coordinator.inflight_count == 0
    with pytest.raises(HistoricalSourceError):
        await coordinator.fetch(plan)  # retry issues a fresh call
    assert source.call_count == 2


async def test_cancelling_one_waiter_does_not_abort_the_shared_fetch() -> None:
    source = FakeHistoricalSource(direct_timeframes=frozenset({_FIVE}), default=Behavior.BLOCK)
    coordinator = _coordinator(source)
    plan = _plan()
    first = asyncio.create_task(coordinator.fetch(plan))
    second = asyncio.create_task(coordinator.fetch(plan))
    await source.wait_until_active(1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    source.release_all()
    result = await second
    assert len(result) > 0
    assert source.call_count == 1
    assert coordinator.inflight_count == 0


async def test_malformed_result_is_not_cached() -> None:
    source = FakeHistoricalSource(
        direct_timeframes=frozenset({_FIVE}), default=Behavior.MALFORMED_OVERLAP
    )
    coordinator = _coordinator(source)
    plan = _plan()
    from app.market_engine.historical.source import HistoricalDataQualityError

    with pytest.raises(HistoricalDataQualityError):
        await coordinator.fetch(plan)
    assert coordinator.inflight_count == 0
    # a second attempt calls the source again (nothing was cached)
    with pytest.raises(HistoricalDataQualityError):
        await coordinator.fetch(plan)
    assert source.call_count == 2
