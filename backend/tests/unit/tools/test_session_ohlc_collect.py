"""DEPLOY-10 R4C collector tests: bounded coverage and live continuity capture.

Offline and deterministic — a fake adapter stands in for Dhan, so no network and no real
sockets. Covers: identity coverage accumulates over the window, stops on full coverage, stops
at a deterministic deadline while persisting nothing it did not see, duplicates do not satisfy
missing identities; and the late-start / reconnect diagnostic capture paths (bounded, clean
stream shutdown, production adapter connect/disconnect never touched by the capture core).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from app.schemas.market_data import (
    Instrument,
    MarketDataKind,
    ProviderSessionOhlc,
    SessionStatisticsObservation,
    SubscriptionRequest,
    Tick,
)
from app.tools.session_ohlc_evidence.collect import (
    capture_late_start,
    capture_reconnect,
    sample_ws,
)

_D = date(2026, 8, 31)
_T0 = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)


def _inst(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str, o: str, h: str, low: str, when: datetime = _T0) -> Tick:
    return Tick(
        instrument=_inst(symbol),
        event_timestamp=when,
        last_price=Decimal(o),
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal(o),
            high_price=Decimal(h),
            low_price=Decimal(low),
            close_price=Decimal(o),
        ),
    )


def _obs(symbol: str, o: str, h: str, low: str) -> SessionStatisticsObservation:
    return SessionStatisticsObservation(
        instrument=_inst(symbol),
        trading_date=_D,
        observed_at=_T0,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal(o),
            high_price=Decimal(h),
            low_price=Decimal(low),
            close_price=Decimal(o),
        ),
    )


class _FakeAdapter:
    """Minimal read-only stand-in: scripted WS streams and REST snapshots.

    ``ws_calls`` is one tick list per ``stream_market_data`` call; ``hang`` makes each stream
    block after emitting its ticks (so a bounded deadline, not an early stream end, is what
    stops collection). ``connect``/``disconnect`` raise — the capture core must never call them.
    """

    def __init__(
        self,
        ws_calls: Sequence[Sequence[Tick]],
        *,
        rest: Sequence[SessionStatisticsObservation] = (),
        hang: bool = False,
    ) -> None:
        self._ws_calls = [list(call) for call in ws_calls]
        self._rest = list(rest)
        self._hang = hang
        self.stream_open_count = 0
        self.stream_close_count = 0

    def stream_market_data(self, request: SubscriptionRequest):
        ticks = self._ws_calls.pop(0) if self._ws_calls else []
        return self._gen(ticks)

    async def _gen(self, ticks: list[Tick]):
        self.stream_open_count += 1
        try:
            for tick in ticks:
                yield tick
            if self._hang:
                await asyncio.Event().wait()
        finally:
            self.stream_close_count += 1

    async def load_session_statistics(
        self, instruments, *, trading_date, observed_at
    ) -> tuple[SessionStatisticsObservation, ...]:
        return tuple(self._rest)

    async def connect(self) -> None:
        raise AssertionError("capture core must not connect the adapter")

    async def disconnect(self) -> None:
        raise AssertionError("capture core must not disconnect the adapter")


def _request(*symbols: str) -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=tuple(_inst(s) for s in symbols),
        data_types=frozenset({MarketDataKind.TICK}),
    )


# --------------------------------------------------------------------------- #
# Coverage — sample_ws bounded accumulation
# --------------------------------------------------------------------------- #
async def test_cov_ws_01_accumulates_and_stops_on_full_coverage() -> None:
    adapter = _FakeAdapter([[_tick("AAA", "100", "101", "99"), _tick("BBB", "50", "51", "49")]])
    out = await sample_ws(
        adapter, _request("AAA", "BBB"), window="early", expected=2, deadline_seconds=5.0
    )
    assert set(out) == {"NSE:AAA", "NSE:BBB"}
    assert adapter.stream_close_count == 1  # clean shutdown on early break


async def test_cov_ws_02_later_instrument_within_window_counts() -> None:
    # BBB arrives after AAA in the same stream; coverage must accumulate to full.
    adapter = _FakeAdapter(
        [[_tick("AAA", "100", "101", "99"), _tick("BBB", "50", "55", "49")]], hang=True
    )
    out = await sample_ws(
        adapter, _request("AAA", "BBB"), window="early", expected=2, deadline_seconds=5.0
    )
    assert set(out) == {"NSE:AAA", "NSE:BBB"}


async def test_cov_ws_03_deadline_stops_with_partial_and_no_phantom() -> None:
    adapter = _FakeAdapter([[_tick("AAA", "100", "101", "99")]], hang=True)
    out = await sample_ws(
        adapter, _request("AAA", "BBB"), window="early", expected=2, deadline_seconds=0.05
    )
    assert set(out) == {"NSE:AAA"}  # BBB never seen; not fabricated
    assert adapter.stream_close_count == 1  # stream closed after the deadline


async def test_cov_ws_04_duplicate_does_not_satisfy_missing() -> None:
    adapter = _FakeAdapter(
        [[_tick("AAA", "100", "101", "99"), _tick("AAA", "100", "102", "98")]], hang=True
    )
    out = await sample_ws(
        adapter, _request("AAA", "BBB"), window="early", expected=2, deadline_seconds=0.05
    )
    assert set(out) == {"NSE:AAA"}  # duplicate AAA does not count as BBB


# --------------------------------------------------------------------------- #
# CSOA16 — live late-start capture
# --------------------------------------------------------------------------- #
async def test_capture_late_start_records_raw_prior_and_first() -> None:
    adapter = _FakeAdapter(
        [[_tick("AAA", "100", "105", "96")]], rest=[_obs("AAA", "100", "104", "97")]
    )
    ev = await capture_late_start(
        adapter, _inst("AAA"), trading_date=_D, observed_at=_T0, deadline_seconds=5.0
    )
    assert ev.observed is True
    assert ev.prior_open == Decimal("100") and ev.prior_high == Decimal("104")
    assert ev.first_high == Decimal("105") and ev.first_low == Decimal("96")


async def test_capture_late_start_incomplete_when_no_first_tick() -> None:
    adapter = _FakeAdapter([[]], rest=[_obs("AAA", "100", "104", "97")], hang=True)
    ev = await capture_late_start(
        adapter, _inst("AAA"), trading_date=_D, observed_at=_T0, deadline_seconds=0.05
    )
    assert ev.observed is False


# --------------------------------------------------------------------------- #
# CSOA16 — live reconnect capture (fresh socket each leg)
# --------------------------------------------------------------------------- #
async def test_capture_reconnect_uses_two_fresh_streams() -> None:
    adapter = _FakeAdapter([[_tick("AAA", "100", "104", "97")], [_tick("AAA", "100", "106", "95")]])
    ev = await capture_reconnect(adapter, _inst("AAA"), deadline_seconds=5.0)
    assert ev.observed is True
    assert ev.pre is not None and ev.post is not None
    assert ev.pre.high_price == Decimal("104") and ev.post.high_price == Decimal("106")
    assert adapter.stream_open_count == 2  # pre and post over separate sockets (reconnect)
    assert adapter.stream_close_count == 2  # both closed cleanly


async def test_capture_reconnect_incomplete_when_no_post() -> None:
    adapter = _FakeAdapter([[_tick("AAA", "100", "104", "97")], []], hang=True)
    ev = await capture_reconnect(adapter, _inst("AAA"), deadline_seconds=0.05)
    assert ev.observed is False
