"""Historical fetch coordination: cache, in-flight dedup, and bounded concurrency (P4.5B).

The coordinator is the single entry point for "get me the candles for this plan".
It serves cache hits directly, coalesces identical concurrent plans onto one source
call, and bounds how many source operations run at once (a work bound, not provider
rate limiting — provider pacing stays in the Phase-3 adapter, docs/05 §8). Source
results are verified before caching; malformed data is never cached and surfaces as
a failure that both coalesced awaiters observe (ADR-006 withhold-authority).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.source import (
    HistoricalFetchPlan,
    HistoricalRequestKey,
    HistoricalSource,
    verify_source_candles,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle


class HistoricalCoordinator:
    """Coordinates cached, deduplicated, bounded-concurrency historical fetches."""

    def __init__(
        self, *, source: HistoricalSource, cache: HistoricalCache, max_concurrency: int
    ) -> None:
        """Wire the coordinator to a source and cache with a positive work bound.

        Args:
            source: The engine-local historical source port.
            cache: The exact-coverage candle cache.
            max_concurrency: The maximum number of concurrent source operations.

        Raises:
            ValueError: If ``max_concurrency`` is not positive.
        """
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        self._source = source
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._inflight: dict[HistoricalRequestKey, asyncio.Future[tuple[Candle, ...]]] = {}

    @property
    def direct_timeframes(self) -> frozenset[Timeframe]:
        """Return the timeframes the wired source supports directly."""
        return self._source.direct_timeframes

    @property
    def inflight_count(self) -> int:
        """Return how many source operations are currently in flight (for tests/health)."""
        return len(self._inflight)

    def retain_timeframes(self, active: frozenset[Timeframe]) -> None:
        """Evict cached windows whose timeframe is no longer required (bounded state)."""
        self._cache.retain_timeframes(active)

    async def fetch(self, plan: HistoricalFetchPlan) -> tuple[Candle, ...]:
        """Return authoritative candles for a plan via cache, coalescing, then source.

        Identical concurrent plans share one source call; a cancelled waiter does not
        abort the shared work (it is shielded), so other waiters and the cache still
        receive the result.

        Args:
            plan: The fetch plan to satisfy.

        Returns:
            The authoritative candles for the plan's window (possibly empty).

        Raises:
            HistoricalSourceError: If the source fails.
            HistoricalDataQualityError: If the source returns malformed data.
        """
        cached = self._cache.get(plan.key)
        if cached is not None:
            return cached
        task = self._inflight.get(plan.key)
        if task is None:
            task = asyncio.ensure_future(self._run(plan))
            self._inflight[plan.key] = task
            task.add_done_callback(self._discard(plan.key))
        return await asyncio.shield(task)

    def _discard(
        self, key: HistoricalRequestKey
    ) -> Callable[[asyncio.Future[tuple[Candle, ...]]], None]:
        """Return a done-callback that removes a completed request from the in-flight map."""

        def _remove(_finished: asyncio.Future[tuple[Candle, ...]]) -> None:
            self._inflight.pop(key, None)

        return _remove

    async def _run(self, plan: HistoricalFetchPlan) -> tuple[Candle, ...]:
        """Load from the source under the concurrency bound, verify, and cache."""
        async with self._semaphore:
            result = await self._source.load(plan.request)
        candles = verify_source_candles(plan, result)
        if candles:
            self._cache.put(plan.key, candles)
        return candles
