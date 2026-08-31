"""Read-only live collector for current-session OHLC authority evidence (DEPLOY-10 R4B).

Observes the two Dhan sources for the production universe — WS tick-carried
``Tick.session_ohlc`` and REST ``/marketfeed/ohlc`` (the operational oracle; note both are
Dhan-derived, so agreement proves provider-path consistency, not independent external
truth) — and assembles an :class:`EvidenceRecord`. Diagnostic only: it never publishes
StrategyResults, never mutates MarketContext / InstrumentState / scanner state, and never
flips an authority bit. Executed only during a live session by the R4B procedure.

Tick size is not exposed by the authoritative instrument metadata, so it is passed
explicitly (per run) or left ``None``; a ``None`` tick means high/low differences are
INDETERMINATE (never silently DRIFT). Late-start and reconnect (CSOA16) evidence require a
dedicated diagnostic subscription / observed reconnect; a single straight-through run leaves
them unobserved, so the record evaluates INCONCLUSIVE — the correct honest outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

from app.adapters.dhan.adapter import DhanRestAdapter
from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    SubscriptionRequest,
    Tick,
)
from app.tools.session_ohlc_evidence.canonical import float32_hex
from app.tools.session_ohlc_evidence.evaluate import classify_price, evaluate_monotonicity
from app.tools.session_ohlc_evidence.models import (
    Classification,
    EvidenceRecord,
    InstrumentEvidence,
    LateStartEvidence,
    OhlcObservation,
    OracleComparison,
    ReconnectEvidence,
)

_METHOD_BY_CLASS = {
    Classification.MATCH: "exact",
    Classification.PROTOCOL_EQUIVALENT: "float32",
    Classification.DRIFT: "tick",
    Classification.INDETERMINATE: "unknown_tick",
    Classification.MISMATCH: "none",
}

_COLLECTOR_VERSION = "2.0.0"


def _key(instrument: Instrument) -> str:
    return f"{instrument.exchange}:{instrument.symbol}"


async def sample_ws(
    adapter: DhanRestAdapter,
    request: SubscriptionRequest,
    *,
    window: str,
    expected: int,
    deadline_seconds: float,
) -> dict[str, OhlcObservation]:
    """Collect the latest WS ``session_ohlc`` per instrument within a bounded window."""
    out: dict[str, OhlcObservation] = {}
    stream = cast("AsyncGenerator[MarketData]", adapter.stream_market_data(request))
    try:
        async with asyncio.timeout(deadline_seconds):
            async for datum in stream:
                obs = _ws_observation(datum, window)
                if obs is not None:
                    out[_key(datum.instrument)] = obs
                    if len(out) >= expected:
                        break
    except TimeoutError:
        pass
    finally:
        await stream.aclose()
    return out


def _ws_observation(datum: MarketData, window: str) -> OhlcObservation | None:
    if not isinstance(datum, Tick) or datum.session_ohlc is None:
        return None
    ohlc = datum.session_ohlc
    return OhlcObservation(
        source="ws",
        window=window,
        observed_at=datum.event_timestamp,
        open_price=ohlc.open_price,
        high_price=ohlc.high_price,
        low_price=ohlc.low_price,
    )


async def fetch_rest(
    adapter: DhanRestAdapter,
    instruments: Sequence[Instrument],
    *,
    window: str,
    trading_date: date,
    observed_at: datetime,
) -> dict[str, OhlcObservation]:
    """Fetch the REST ``/marketfeed/ohlc`` oracle observation per instrument."""
    observations = await adapter.load_session_statistics(
        instruments, trading_date=trading_date, observed_at=observed_at
    )
    return {
        _key(obs.instrument): OhlcObservation(
            source="rest",
            window=window,
            observed_at=obs.observed_at,
            trading_date=obs.trading_date,
            open_price=obs.session_ohlc.open_price,
            high_price=obs.session_ohlc.high_price,
            low_price=obs.session_ohlc.low_price,
        )
        for obs in observations
    }


async def capture_late_start(
    adapter: DhanRestAdapter,
    instrument: Instrument,
    *,
    trading_date: date,
    observed_at: datetime,
    deadline_seconds: float,
) -> LateStartEvidence:
    """Capture raw late-start evidence: a pre-subscription REST snapshot vs the first WS obs.

    ``prior_*`` is the oracle's session-to-date extrema taken *before* subscribing; ``first_*``
    is the first post-subscription WS observation. The evaluator derives (never trusts) whether
    the first tick already carried the prior extrema. Bounded by ``deadline_seconds``; the
    diagnostic WS stream is opened and closed by :func:`sample_ws` (clean shutdown).
    """
    rest = await fetch_rest(
        adapter, [instrument], window="prior", trading_date=trading_date, observed_at=observed_at
    )
    prior = rest.get(_key(instrument))
    request = SubscriptionRequest(
        instruments=(instrument,), data_types=frozenset({MarketDataKind.TICK})
    )
    ws = await sample_ws(
        adapter, request, window="first", expected=1, deadline_seconds=deadline_seconds
    )
    first = ws.get(_key(instrument))
    if prior is None or first is None:
        return LateStartEvidence(
            observed=False,
            detail="late-start capture incomplete (prior REST or first WS observation missing)",
        )
    return LateStartEvidence(
        observed=True,
        prior_observed_at=prior.observed_at,
        prior_open=prior.open_price,
        prior_high=prior.high_price,
        prior_low=prior.low_price,
        first_observed_at=first.observed_at,
        first_open=first.open_price,
        first_high=first.high_price,
        first_low=first.low_price,
        detail="prior=REST snapshot before subscription; first=first post-subscription WS tick",
    )


async def capture_reconnect(
    adapter: DhanRestAdapter,
    instrument: Instrument,
    *,
    deadline_seconds: float,
) -> ReconnectEvidence:
    """Capture raw pre/post evidence across a fresh diagnostic socket (a reconnect, CSOA16).

    Two sequential :func:`sample_ws` calls each open and cleanly close their own diagnostic
    stream, so the second observation is taken over a brand-new socket — a reconnect. Continuity
    (session-to-date extrema preserved post-reconnect) is derived by the evaluator, not asserted
    here. Bounded by ``deadline_seconds`` per leg. The production feed socket is never touched.
    """
    request = SubscriptionRequest(
        instruments=(instrument,), data_types=frozenset({MarketDataKind.TICK})
    )
    pre_map = await sample_ws(
        adapter, request, window="pre", expected=1, deadline_seconds=deadline_seconds
    )
    post_map = await sample_ws(
        adapter, request, window="post", expected=1, deadline_seconds=deadline_seconds
    )
    pre = pre_map.get(_key(instrument))
    post = post_map.get(_key(instrument))
    if pre is None or post is None:
        return ReconnectEvidence(
            observed=False,
            detail="reconnect capture incomplete (pre or post observation missing)",
        )
    return ReconnectEvidence(
        observed=True,
        pre=pre,
        post=post,
        detail="pre and post captured across a fresh diagnostic socket (reconnect)",
    )


def _comparisons(
    window: str, ws: OhlcObservation, rest: OhlcObservation, tick_size: Decimal | None
) -> tuple[OracleComparison, ...]:
    fields = (
        ("open", ws.open_price, rest.open_price, True),
        ("high", ws.high_price, rest.high_price, False),
        ("low", ws.low_price, rest.low_price, False),
    )
    comparisons: list[OracleComparison] = []
    for name, ws_v, rest_v, exact in fields:
        classification = classify_price(ws_v, rest_v, tick_size=tick_size, exact=exact)
        comparisons.append(
            OracleComparison(
                window=window,
                field=name,
                ws_value=ws_v,
                rest_value=rest_v,
                tick_size=None if exact else tick_size,
                classification=classification,
                method=_METHOD_BY_CLASS[classification],
                ws_float32_bits=float32_hex(ws_v),
                rest_float32_bits=float32_hex(rest_v),
            )
        )
    return tuple(comparisons)


def build_instrument_evidence(
    instrument: Instrument,
    security_id: str,
    trading_date: date,
    *,
    ws_by_window: dict[str, OhlcObservation],
    rest_by_window: dict[str, OhlcObservation],
    tick_size: Decimal | None,
) -> InstrumentEvidence:
    """Assemble one instrument's multi-window observations, comparisons, and monotonicity."""
    ws_obs = tuple(ws_by_window[w] for w in sorted(ws_by_window))
    rest_obs = tuple(rest_by_window[w] for w in sorted(rest_by_window))
    comparisons: list[OracleComparison] = []
    for window in sorted(set(ws_by_window) & set(rest_by_window)):
        comparisons.extend(
            _comparisons(window, ws_by_window[window], rest_by_window[window], tick_size)
        )
    return InstrumentEvidence(
        exchange=instrument.exchange,
        symbol=instrument.symbol,
        security_id=security_id,
        trading_date=trading_date,
        ws_observations=ws_obs,
        rest_observations=rest_obs,
        oracle_comparisons=tuple(comparisons),
        monotonicity=evaluate_monotonicity(ws_obs),
    )


async def run_collect(
    settings: Settings,
    *,
    windows: Sequence[str],
    trading_date: date,
    session_identity: str,
    source_sha: str,
    tick_size: Decimal | None = None,
    per_window_seconds: float = 30.0,
) -> EvidenceRecord:
    """Run a straight-through read-only collection over the given windows (R4B use only)."""
    adapter = DhanRestAdapter.from_settings(settings)
    await adapter.connect()
    ws_acc: dict[str, dict[str, OhlcObservation]] = {}
    rest_acc: dict[str, dict[str, OhlcObservation]] = {}
    start = datetime.now(UTC)
    try:
        universe = (await _load_universe(adapter)).cash_references
        instruments = tuple(ref.instrument for ref in universe)
        request = SubscriptionRequest(
            instruments=instruments, data_types=frozenset({MarketDataKind.TICK})
        )
        for window in windows:
            observed_at = datetime.now(UTC)
            ws = await sample_ws(
                adapter,
                request,
                window=window,
                expected=len(instruments),
                deadline_seconds=per_window_seconds,
            )
            rest = await fetch_rest(
                adapter,
                instruments,
                window=window,
                trading_date=trading_date,
                observed_at=observed_at,
            )
            for key, obs in ws.items():
                ws_acc.setdefault(key, {})[window] = obs
            for key, obs in rest.items():
                rest_acc.setdefault(key, {})[window] = obs
    finally:
        await adapter.disconnect()
    return _assemble(
        universe,
        ws_acc,
        rest_acc,
        trading_date=trading_date,
        session_identity=session_identity,
        source_sha=source_sha,
        tick_size=tick_size,
        windows=windows,
        start=start,
    )


async def _load_universe(adapter: DhanRestAdapter) -> DhanCashEquityLiveUniverse:
    await adapter.load_instruments()
    return adapter.load_nse_cash_equity_live_universe()


def _find_ref(universe: Sequence[DhanInstrumentReference], symbol: str) -> DhanInstrumentReference:
    for ref in universe:
        if ref.instrument.symbol == symbol:
            return ref
    raise ValueError(f"symbol {symbol!r} not in the authoritative live universe")


def _partial_record(
    universe: Sequence[DhanInstrumentReference],
    *,
    trading_date: date,
    session_identity: str,
    source_sha: str,
    start: datetime,
    late_start: LateStartEvidence | None,
    reconnect: ReconnectEvidence | None,
) -> EvidenceRecord:
    """A window-less partial record carrying only continuity evidence, for later ``combine``."""
    expected_ids = tuple(_key(ref.instrument) for ref in universe)
    return EvidenceRecord(
        collector_version=_COLLECTOR_VERSION,
        source_sha=source_sha,
        provider="dhan",
        trading_date=trading_date,
        session_identity=session_identity,
        collection_start=start,
        collection_end=datetime.now(UTC),
        expected_instruments=expected_ids,
        pending_instruments=expected_ids,
        sample_windows=(),
        instruments=(),
        late_start=late_start,
        reconnect=reconnect,
    )


async def run_capture_late_start(
    settings: Settings,
    *,
    symbol: str,
    trading_date: date,
    session_identity: str,
    source_sha: str,
    deadline_seconds: float = 60.0,
) -> EvidenceRecord:
    """Run a read-only diagnostic late-start capture for one instrument (R4D use only)."""
    adapter = DhanRestAdapter.from_settings(settings)
    await adapter.connect()
    start = datetime.now(UTC)
    try:
        universe = (await _load_universe(adapter)).cash_references
        ref = _find_ref(universe, symbol)
        late = await capture_late_start(
            adapter,
            ref.instrument,
            trading_date=trading_date,
            observed_at=datetime.now(UTC),
            deadline_seconds=deadline_seconds,
        )
    finally:
        await adapter.disconnect()
    return _partial_record(
        universe,
        trading_date=trading_date,
        session_identity=session_identity,
        source_sha=source_sha,
        start=start,
        late_start=late,
        reconnect=None,
    )


async def run_capture_reconnect(
    settings: Settings,
    *,
    symbol: str,
    trading_date: date,
    session_identity: str,
    source_sha: str,
    deadline_seconds: float = 60.0,
) -> EvidenceRecord:
    """Run a read-only diagnostic reconnect capture for one instrument (R4D use only)."""
    adapter = DhanRestAdapter.from_settings(settings)
    await adapter.connect()
    start = datetime.now(UTC)
    try:
        universe = (await _load_universe(adapter)).cash_references
        ref = _find_ref(universe, symbol)
        reconnect = await capture_reconnect(
            adapter, ref.instrument, deadline_seconds=deadline_seconds
        )
    finally:
        await adapter.disconnect()
    return _partial_record(
        universe,
        trading_date=trading_date,
        session_identity=session_identity,
        source_sha=source_sha,
        start=start,
        late_start=None,
        reconnect=reconnect,
    )


def _assemble(
    universe: Sequence[DhanInstrumentReference],
    ws_acc: dict[str, dict[str, OhlcObservation]],
    rest_acc: dict[str, dict[str, OhlcObservation]],
    *,
    trading_date: date,
    session_identity: str,
    source_sha: str,
    tick_size: Decimal | None,
    windows: Sequence[str],
    start: datetime,
) -> EvidenceRecord:
    expected_ids = tuple(_key(ref.instrument) for ref in universe)
    pending_ids = tuple(key for key in expected_ids if key not in ws_acc)
    instruments_evidence: list[InstrumentEvidence] = []
    for ref in universe:
        instrument = ref.instrument
        key = _key(instrument)
        if key not in ws_acc and key not in rest_acc:
            continue
        instruments_evidence.append(
            build_instrument_evidence(
                instrument,
                str(ref.security_id),
                trading_date,
                ws_by_window=ws_acc.get(key, {}),
                rest_by_window=rest_acc.get(key, {}),
                tick_size=tick_size,
            )
        )
    return EvidenceRecord(
        collector_version=_COLLECTOR_VERSION,
        source_sha=source_sha,
        provider="dhan",
        trading_date=trading_date,
        session_identity=session_identity,
        collection_start=start,
        collection_end=datetime.now(UTC),
        expected_instruments=expected_ids,
        pending_instruments=pending_ids,
        sample_windows=tuple(windows),
        instruments=tuple(instruments_evidence),
        late_start=LateStartEvidence(observed=False, detail="not captured by straight-through run"),
        reconnect=ReconnectEvidence(observed=False, detail="no reconnect observed"),
    )
