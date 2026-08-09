"""A deterministic in-memory fake HistoricalSource for P4.5B tests.

Generates authoritative candles aligned to the requested window, and supports
controlled failure, controlled blocking (via asyncio primitives, no sleeps),
malformed output, and call/concurrency accounting — so cache, coalescing,
bounded-concurrency, and warmup behaviour can be asserted deterministically.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum, auto

from app.market_engine.historical.source import HistoricalSourceError
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, HistoricalRequest, HistoricalResult, Instrument

_MAX_CANDLES = 5000


class Behavior(StrEnum):
    """How the fake source responds to a matching request."""

    NORMAL = auto()
    FAIL = auto()
    BLOCK = auto()
    BLOCK_THEN_FAIL = auto()
    INSUFFICIENT = auto()
    MALFORMED_FOREIGN = auto()
    MALFORMED_OVERLAP = auto()
    MALFORMED_DUPLICATE = auto()


class FakeHistoricalSource:
    """A configurable, deterministic historical source test double."""

    def __init__(
        self,
        *,
        direct_timeframes: frozenset[Timeframe],
        by_symbol: dict[str, Behavior] | None = None,
        by_interval: dict[timedelta, Behavior] | None = None,
        default: Behavior = Behavior.NORMAL,
    ) -> None:
        self._direct = direct_timeframes
        self._by_symbol = by_symbol or {}
        self._by_interval = by_interval or {}
        self._default = default
        self.call_count = 0
        self.calls_by_interval: dict[timedelta, int] = {}
        self.max_active = 0
        self._active = 0
        self._gate = asyncio.Event()
        self._cond = asyncio.Condition()

    @property
    def direct_timeframes(self) -> frozenset[Timeframe]:
        return self._direct

    def release_all(self) -> None:
        """Release every load currently blocked on the gate."""
        self._gate.set()

    async def wait_until_active(self, count: int) -> None:
        """Block until at least ``count`` loads are concurrently active."""
        async with self._cond:
            await self._cond.wait_for(lambda: self._active >= count)

    async def load(self, request: HistoricalRequest) -> HistoricalResult:
        behavior = self._behavior_for(request)
        async with self._cond:
            self.call_count += 1
            self.calls_by_interval[request.interval] = (
                self.calls_by_interval.get(request.interval, 0) + 1
            )
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self._cond.notify_all()
        try:
            if behavior in (Behavior.BLOCK, Behavior.BLOCK_THEN_FAIL):
                await self._gate.wait()
            if behavior in (Behavior.FAIL, Behavior.BLOCK_THEN_FAIL):
                raise HistoricalSourceError("configured source failure")
            return HistoricalResult(request=request, candles=self._candles_for(request, behavior))
        finally:
            async with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def _behavior_for(self, request: HistoricalRequest) -> Behavior:
        symbol = request.instrument.symbol
        if symbol in self._by_symbol:
            return self._by_symbol[symbol]
        if request.interval in self._by_interval:
            return self._by_interval[request.interval]
        return self._default

    def _candles_for(self, request: HistoricalRequest, behavior: Behavior) -> tuple[Candle, ...]:
        if behavior is Behavior.MALFORMED_FOREIGN:
            return self._foreign(request)
        if behavior is Behavior.MALFORMED_OVERLAP:
            return self._overlap(request)
        if behavior is Behavior.MALFORMED_DUPLICATE:
            return self._duplicate(request)
        generated = self._generate(request)
        if behavior is Behavior.INSUFFICIENT:
            return generated[:1]
        return generated

    @staticmethod
    def _candle(instrument: Instrument, start: object, end: object) -> Candle:
        return Candle(
            instrument=instrument,
            start_timestamp=start,  # type: ignore[arg-type]
            end_timestamp=end,  # type: ignore[arg-type]
            open_price=Decimal("100"),
            high_price=Decimal("101"),
            low_price=Decimal("99"),
            close_price=Decimal("100"),
            traded_quantity=10,
        )

    def _generate(self, request: HistoricalRequest) -> tuple[Candle, ...]:
        candles: list[Candle] = []
        cursor = request.start_timestamp
        while cursor < request.end_timestamp and len(candles) < _MAX_CANDLES:
            candles.append(self._candle(request.instrument, cursor, cursor + request.interval))
            cursor = cursor + request.interval
        return tuple(candles)

    def _foreign(self, request: HistoricalRequest) -> tuple[Candle, ...]:
        other = request.instrument.model_copy(update={"symbol": f"{request.instrument.symbol}X"})
        start = request.start_timestamp
        return (self._candle(other, start, start + request.interval),)

    def _overlap(self, request: HistoricalRequest) -> tuple[Candle, ...]:
        start = request.start_timestamp
        width = request.interval * 2
        return (
            self._candle(request.instrument, start, start + width),
            self._candle(
                request.instrument, start + request.interval, start + width + request.interval
            ),
        )

    def _duplicate(self, request: HistoricalRequest) -> tuple[Candle, ...]:
        start = request.start_timestamp
        candle = self._candle(request.instrument, start, start + request.interval)
        return (candle, candle)
