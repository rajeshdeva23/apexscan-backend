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
from app.tools.session_ohlc_evidence.evaluate import classify_price, evaluate_monotonicity
from app.tools.session_ohlc_evidence.models import (
    EvidenceRecord,
    InstrumentEvidence,
    LateStartEvidence,
    OhlcObservation,
    OracleComparison,
    ReconnectEvidence,
)

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


def _comparisons(
    window: str, ws: OhlcObservation, rest: OhlcObservation, tick_size: Decimal | None
) -> tuple[OracleComparison, ...]:
    fields = (
        ("open", ws.open_price, rest.open_price, True),
        ("high", ws.high_price, rest.high_price, False),
        ("low", ws.low_price, rest.low_price, False),
    )
    return tuple(
        OracleComparison(
            window=window,
            field=name,
            ws_value=ws_v,
            rest_value=rest_v,
            tick_size=None if exact else tick_size,
            classification=classify_price(ws_v, rest_v, tick_size=tick_size, exact=exact),
        )
        for name, ws_v, rest_v, exact in fields
    )


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
        sample_windows=tuple(windows),
        instruments=tuple(instruments_evidence),
        late_start=LateStartEvidence(observed=False, detail="not captured by straight-through run"),
        reconnect=ReconnectEvidence(observed=False, detail="no reconnect observed"),
    )
