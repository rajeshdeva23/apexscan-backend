"""In-process, read-only Session-OHLC evidence observer (ADR-015; DEPLOY-10 R4D).

Single-account architecture: this observer reuses the existing production Dhan MarketFeed
(via the shared :class:`EventBus` ``MarketContext`` events) and the existing authenticated
provider (via an injected :class:`SessionStatisticsSource`). It NEVER opens a second Dhan
WebSocket, constructs another adapter/auth manager, generates or renews a token, or mutates
any Market-Engine state. It only collects schema-2.2.0 evidence; the existing evaluator
decides, and authority is untouched (ADR-009 CSOA20).

Isolation: the bus is synchronous and propagates subscriber exceptions (``app/events/bus.py``),
so the registered callback is a non-throwing boundary that does only O(1) work — extract a
minimal immutable snapshot per instrument. All REST, evaluation, serialization, and disk I/O
happen off the callback, at governed window boundaries, and every window is gated by the
authoritative session classifier (never during ``MARKET_CLOSED``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.base.broker_adapter import SessionStatisticsSource
from app.events.bus import EventBus, Subscription
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.schemas.market_data import FeedContinuity, FeedContinuityEvent, Instrument
from app.tools.session_ohlc_evidence.evaluate import (
    classify_price,
    combine_records,
    evaluate_monotonicity,
    evaluate_record,
)
from app.tools.session_ohlc_evidence.models import (
    EvidenceRecord,
    InstrumentEvidence,
    LateStartEvidence,
    LateStartKind,
    OhlcObservation,
    OracleComparison,
    ReconnectEvidence,
    Verdict,
)
from app.tools.session_ohlc_evidence.report import to_json, to_markdown

_LOGGER = logging.getLogger(__name__)
_COLLECTOR_VERSION = "3.0.0"
_REQUIRED_WINDOWS = ("early", "mid", "late")

# Per-window lifecycle (ADR-015 B7): a window is COLLECTING while it accumulates passive
# observations and only becomes FINALIZED (exactly one artifact) on full frozen-universe
# coverage or at the governed deadline. Absence of an entry == NOT_STARTED. Window eligibility
# must NOT mean immediate snapshot.
_PHASE_COLLECTING = "collecting"
_PHASE_FINALIZED = "finalized"

_METHOD_BY_CLASS = {
    "match": "exact",
    "protocol_equivalent": "float32",
    "drift": "tick",
    "indeterminate": "unknown_tick",
    "mismatch": "none",
}

ClassifierPort = Callable[[datetime], SessionContext]
ClockPort = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Minimal immutable per-instrument evidence values copied off the frozen MarketContext."""

    identity: str
    exchange: str
    symbol: str
    trading_date: date | None
    event_timestamp: datetime
    observed_at: datetime
    version: int
    open_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None


@dataclass(frozen=True, slots=True)
class WindowResult:
    """Outcome of a single governed window capture."""

    window: str
    executed: bool
    reason: str
    observed: int = 0
    pending: int = 0


def _identity(instrument: Instrument) -> str:
    return f"{instrument.exchange}:{instrument.symbol}"


_WINDOW_CADENCE: tuple[tuple[str, time, time], ...] = (
    ("early", time(9, 20), time(9, 35)),
    ("mid", time(12, 0), time(12, 30)),
    ("late", time(15, 0), time(15, 20)),
)


def _due_window(local_time: time) -> str | None:
    """Map an exchange-local time to its governed window label, or ``None`` if outside all."""
    for label, start, end in _WINDOW_CADENCE:
        if start <= local_time <= end:
            return label
    return None


class SessionOhlcEvidenceObserver:
    """Passive, read-only collector of current-session OHLC evidence (ADR-015).

    Construct only when evidence collection is enabled; when the runtime flag is off the
    observer is never created, so there is zero subscription, REST, artifact, or behavioral
    difference.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        classifier: ClassifierPort,
        clock: ClockPort,
        statistics_source: SessionStatisticsSource,
        universe: Sequence[Instrument],
        artifact_root: Path,
        source_sha: str,
        production_image: str | None,
        exchange_timezone: str = "Asia/Kolkata",
        session_identity: str = "regular",
        per_window_seconds: float = 300.0,
        poll_seconds: float = 30.0,
    ) -> None:
        self._bus = bus
        self._classify = classifier
        self._now = clock
        self._source = statistics_source
        self._universe = tuple(universe)
        self._artifact_root = artifact_root
        self._source_sha = source_sha
        self._production_image = production_image
        self._exchange_timezone = exchange_timezone
        self._session_identity = session_identity
        self._per_window_seconds = per_window_seconds
        self._poll_seconds = poll_seconds
        self._finalized = False

        self._subscriptions: list[Subscription[Any]] = []
        self._latest: dict[str, _Snapshot] = {}
        self._partials: list[EvidenceRecord] = []
        self._window_phase: dict[
            str, str
        ] = {}  # window -> COLLECTING | FINALIZED (absent = not started)
        self._window_deadline: dict[str, datetime] = {}
        self._session_trading_date: date | None = None
        self._frozen_universe: tuple[str, ...] | None = None
        self._attach_timestamp: datetime | None = None
        self._continuity: list[FeedContinuityEvent] = []
        self._pre_reconnect: dict[str, _Snapshot] | None = None
        self._reconnect: ReconnectEvidence | None = None

    # ------------------------------------------------------------------ #
    # Subscription lifecycle
    # ------------------------------------------------------------------ #
    def subscribe(self) -> None:
        """Attach the read-only bus handlers (idempotent). Records the attach instant."""
        if self._subscriptions:
            return
        self._attach_timestamp = self._safe_now()
        self._subscriptions.append(self._bus.subscribe(MarketContextCreated, self._on_context))
        self._subscriptions.append(self._bus.subscribe(MarketContextUpdated, self._on_context))

    def unsubscribe(self) -> None:
        """Detach all bus handlers (idempotent)."""
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions.clear()

    # ------------------------------------------------------------------ #
    # Window driver (classifier-gated cadence; cancellable)
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Poll the classifier and capture EARLY/MID/LATE at their cadence; finalize at close."""
        while True:
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a window failure must never end the driver or touch ingestion
                _LOGGER.warning("evidence observer window tick skipped", exc_info=True)
            await asyncio.sleep(self._poll_seconds)

    async def _tick_once(self) -> None:
        """One driver poll: advance COLLECTING windows, then open a newly-eligible window.

        Opening a window only begins COLLECTING — it never snapshots immediately. A window is
        finalized (exactly once) on full frozen-universe coverage or at its governed deadline.
        """
        session = self._classify(self._now())
        if session.market_state is not MarketState.LIVE_SESSION:
            await self._finalize_all_collecting()  # session ending is a hard deadline
            if any(p == _PHASE_FINALIZED for p in self._window_phase.values()) and (
                not self._finalized
            ):
                self.finalize_session()
                self._finalized = True
            return
        self._roll_session(session.trading_date)
        now = self._now()
        await self._advance_collecting(session.trading_date, now)
        band = _due_window(now.astimezone(ZoneInfo(self._exchange_timezone)).time())
        if band is not None and band not in self._window_phase:
            self._begin_collecting(band, now)  # NOT_STARTED -> COLLECTING (no capture yet)

    def _begin_collecting(self, window: str, now: datetime) -> None:
        """Open a window for accumulation; freeze the deadline. Does NOT finalize."""
        self._window_phase[window] = _PHASE_COLLECTING
        self._window_deadline[window] = now + timedelta(seconds=self._per_window_seconds)

    def _coverage_complete(self) -> bool:
        """True iff every frozen-expected identity has a live observation."""
        frozen = set(self._frozen_universe or ())
        if not frozen:
            return False
        return frozen.issubset(self._latest.keys())

    async def _advance_collecting(self, trading_date: date, now: datetime) -> None:
        """Finalize each COLLECTING window that reached full coverage or its deadline."""
        for window in [w for w, p in self._window_phase.items() if p == _PHASE_COLLECTING]:
            if self._coverage_complete() or now >= self._window_deadline.get(window, now):
                await self._finalize_window(window, trading_date)

    async def _finalize_all_collecting(self) -> None:
        """Finalize any still-collecting windows (e.g. at session close)."""
        if self._session_trading_date is None:
            return
        for window in [w for w, p in self._window_phase.items() if p == _PHASE_COLLECTING]:
            await self._finalize_window(window, self._session_trading_date)

    async def _finalize_window(self, window: str, trading_date: date) -> None:
        """Snapshot, REST-compare, and persist one window exactly once (write before mark).

        The window is marked FINALIZED only after the artifact is atomically committed
        (B7 fix for WINDOW_MARK_BEFORE_ARTIFACT_WRITE). A write failure leaves the window
        COLLECTING so it is retried on a later poll; since session OHLC is monotonic and
        coverage only grows, a retry captures an equal-or-more-complete snapshot, never a
        cherry-picked favorable one. If it never commits, the window stays absent (the
        evaluator then treats the missing window as INCONCLUSIVE — fail closed).
        """
        if self._window_phase.get(window) == _PHASE_FINALIZED:
            return
        ws_snapshots = dict(self._latest)
        rest = await self._load_rest(trading_date, self._now())
        record = self._build_partial(
            window, trading_date, self._frozen_universe or (), ws_snapshots, rest
        )
        self._write_record_json(trading_date, window, record)  # atomic; raises on failure
        self._partials.append(record)
        self._window_phase[window] = _PHASE_FINALIZED  # only after the artifact is committed

    # ------------------------------------------------------------------ #
    # Hot path — non-throwing O(1) boundary (ADR-015 D2/D3)
    # ------------------------------------------------------------------ #
    def _on_context(self, event: MarketContextCreated | MarketContextUpdated) -> None:
        """Bus callback: never raises, never does REST/disk/evaluation/serialization."""
        try:
            self._record_snapshot(event.context)
        except Exception:  # evidence must never break ingestion (bus has no isolation)
            _LOGGER.warning("evidence observer snapshot skipped", exc_info=True)

    def _record_snapshot(self, context: MarketContext) -> None:
        tick = context.latest_tick
        ohlc = tick.session_ohlc if tick is not None else None
        snapshot = _Snapshot(
            identity=_identity(context.instrument),
            exchange=context.instrument.exchange,
            symbol=context.instrument.symbol,
            trading_date=context.session.trading_date if context.session is not None else None,
            event_timestamp=context.event_timestamp,
            observed_at=context.observed_at,
            version=context.version,
            open_price=ohlc.open_price if ohlc is not None else None,
            high_price=ohlc.high_price if ohlc is not None else None,
            low_price=ohlc.low_price if ohlc is not None else None,
        )
        self._latest[snapshot.identity] = snapshot  # O(1) replace; state is O(universe)

    # ------------------------------------------------------------------ #
    # Continuity (CSOA16, natural reconnect only)
    # ------------------------------------------------------------------ #
    def on_feed_continuity(self, event: FeedContinuityEvent) -> None:
        """Observe (never cause) CONNECTED/DISCONNECTED/RECONNECTED transitions."""
        try:
            self._continuity.append(event)
            if event.status is FeedContinuity.DISCONNECTED:
                self._pre_reconnect = dict(self._latest)  # freeze last-known pre-reconnect state
            elif event.status is FeedContinuity.RECONNECTED and self._pre_reconnect is not None:
                self._capture_reconnect(event)
        except Exception:  # never propagate into the provider/runtime
            _LOGGER.warning("evidence observer continuity skipped", exc_info=True)

    def _capture_reconnect(self, reconnected: FeedContinuityEvent) -> None:
        pre_map = self._pre_reconnect or {}
        # deterministic representative: lexicographically-first identity present both sides
        for key in sorted(pre_map):
            post = self._latest.get(key)
            pre = pre_map.get(key)
            if post is None or pre is None:
                continue
            self._reconnect = ReconnectEvidence(
                observed=True,
                pre=_to_observation(pre, window="pre"),
                post=_to_observation(post, window="post"),
                detail=f"natural reconnect at {reconnected.observed_at.isoformat()}",
            )
            break
        self._pre_reconnect = None

    # ------------------------------------------------------------------ #
    # Window capture (off the hot path; classifier-gated)
    # ------------------------------------------------------------------ #
    async def capture_window(self, window: str) -> WindowResult:
        """Finalize one governed window NOW from the current accumulated snapshot state.

        This is the finalize primitive (classifier-gated, universe-frozen, REST-compared,
        write-before-mark). The window driver drives *when* this is invoked via the COLLECTING
        state machine (full coverage or deadline); it is never called on a window's first
        eligible poll. Refuses if the window is already FINALIZED (duplicate-window guard).
        """
        session = self._classify(self._now())
        if session.market_state is not MarketState.LIVE_SESSION:
            return WindowResult(window, executed=False, reason="not LIVE_SESSION")
        self._roll_session(session.trading_date)
        if self._window_phase.get(window) == _PHASE_FINALIZED:
            return WindowResult(window, executed=False, reason="window already captured")
        await self._finalize_window(window, session.trading_date)
        record = self._partials[-1]
        return WindowResult(
            window,
            executed=True,
            reason="captured",
            observed=len(record.instruments),
            pending=len(record.pending_instruments),
        )

    def _roll_session(self, trading_date: date) -> None:
        if self._session_trading_date == trading_date:
            return
        # new trading date: finalize nothing implicitly; freeze the universe and reset accumulators
        self._session_trading_date = trading_date
        self._frozen_universe = tuple(sorted(_identity(i) for i in self._universe))
        self._partials.clear()
        self._window_phase.clear()
        self._window_deadline.clear()
        self._reconnect = None

    async def _load_rest(
        self, trading_date: date, observed_at: datetime
    ) -> dict[str, OhlcObservation]:
        try:
            observations = await self._source.load_session_statistics(
                self._universe, trading_date=trading_date, observed_at=observed_at
            )
        except Exception:  # REST failure is evidence incompleteness, never ingestion failure
            _LOGGER.warning("evidence observer REST load failed", exc_info=True)
            return {}
        return {
            _identity(obs.instrument): OhlcObservation(
                source="rest",
                window="rest",
                observed_at=obs.observed_at,
                trading_date=obs.trading_date,
                open_price=obs.session_ohlc.open_price,
                high_price=obs.session_ohlc.high_price,
                low_price=obs.session_ohlc.low_price,
            )
            for obs in observations
        }

    def _build_partial(
        self,
        window: str,
        trading_date: date,
        expected: tuple[str, ...],
        ws_snapshots: dict[str, _Snapshot],
        rest: dict[str, OhlcObservation],
    ) -> EvidenceRecord:
        instruments: list[InstrumentEvidence] = []
        for key in sorted(ws_snapshots):
            snap = ws_snapshots[key]
            ws_obs = _to_observation(snap, window=window)
            comparisons = _comparisons(window, ws_obs, rest.get(key))
            instruments.append(
                InstrumentEvidence(
                    exchange=snap.exchange,
                    symbol=snap.symbol,
                    security_id=key,
                    trading_date=trading_date,
                    ws_observations=(
                        ws_obs,
                    ),  # one per window; combine_records unions across windows
                    rest_observations=(rest[key],) if key in rest else (),
                    oracle_comparisons=comparisons,
                    monotonicity=evaluate_monotonicity((ws_obs,)),
                )
            )
        observed_ids = set(ws_snapshots)
        pending = tuple(k for k in expected if k not in observed_ids)
        now = self._now()
        return EvidenceRecord(
            collector_version=_COLLECTOR_VERSION,
            source_sha=self._source_sha,
            production_image=self._production_image,
            provider="dhan",
            trading_date=trading_date,
            session_identity=self._session_identity,
            collection_start=now,
            collection_end=now,
            expected_instruments=expected,
            pending_instruments=pending,
            sample_windows=(window,),
            instruments=tuple(instruments),
            late_start=self._late_start_for(window, rest),
            reconnect=None,
        )

    def _late_start_for(
        self, window: str, rest: dict[str, OhlcObservation]
    ) -> LateStartEvidence | None:
        if window != _REQUIRED_WINDOWS[0]:
            return None
        first = next((self._latest[k] for k in sorted(self._latest)), None)
        if first is None:
            return LateStartEvidence(
                observed=False,
                kind=LateStartKind.OBSERVER_LATE_ATTACH,
                observer_attach_timestamp=self._attach_timestamp,
                detail="observer attached; no instrument observed yet at first window",
            )
        prior = rest.get(first.identity)
        return LateStartEvidence(
            observed=True,
            kind=LateStartKind.OBSERVER_LATE_ATTACH,
            observer_attach_timestamp=self._attach_timestamp,
            prior_observed_at=prior.observed_at if prior else None,
            prior_open=prior.open_price if prior else None,
            prior_high=prior.high_price if prior else None,
            prior_low=prior.low_price if prior else None,
            first_observed_at=first.event_timestamp,
            first_open=first.open_price,
            first_high=first.high_price,
            first_low=first.low_price,
            detail=(
                "observer_late_attach: extrema seen at first observation reflect production's "
                "long-lived subscription, NOT a provider late-subscription re-send (ADR-015 D7); "
                "provider late-start remains inconclusive"
            ),
        )

    # ------------------------------------------------------------------ #
    # Finalization
    # ------------------------------------------------------------------ #
    def finalize_session(self) -> EvidenceRecord | None:
        """Combine per-window partials, attach reconnect provenance, evaluate, and persist."""
        if not self._partials or self._session_trading_date is None:
            return None
        combined = combine_records(self._partials)
        reconnect = self._reconnect or ReconnectEvidence(
            observed=False, detail="INCONCLUSIVE_NO_NATURAL_RECONNECT"
        )
        combined = combined.model_copy(update={"reconnect": reconnect})
        verdict = evaluate_record(combined)
        self._write_outputs(self._session_trading_date, "combined", combined, verdict)
        return combined

    # ------------------------------------------------------------------ #
    # Artifact writing (atomic; never from the hot path)
    # ------------------------------------------------------------------ #
    def _session_dir(self, trading_date: date) -> Path:
        path = self._artifact_root / trading_date.isoformat()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_record_json(self, trading_date: date, window: str, record: EvidenceRecord) -> None:
        payload = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True, default=str)
        _atomic_write(self._session_dir(trading_date) / f"{window}.json", payload)

    def _write_outputs(
        self, trading_date: date, stem: str, record: EvidenceRecord, verdict: Verdict
    ) -> None:
        directory = self._session_dir(trading_date)
        _atomic_write(directory / f"{stem}.json", to_json(record, verdict))
        _atomic_write(directory / f"{stem}.md", to_markdown(record, verdict))

    def _safe_now(self) -> datetime:
        try:
            return self._now()
        except Exception:
            return datetime.now(UTC)


def _to_observation(snapshot: _Snapshot, *, window: str) -> OhlcObservation:
    return OhlcObservation(
        source="ws",
        window=window,
        observed_at=snapshot.event_timestamp,
        trading_date=snapshot.trading_date,
        open_price=snapshot.open_price,
        high_price=snapshot.high_price,
        low_price=snapshot.low_price,
    )


def _comparisons(
    window: str, ws: OhlcObservation, rest: OhlcObservation | None
) -> tuple[OracleComparison, ...]:
    if rest is None:
        return ()
    fields = (
        ("open", ws.open_price, rest.open_price, True),
        ("high", ws.high_price, rest.high_price, False),
        ("low", ws.low_price, rest.low_price, False),
    )
    out: list[OracleComparison] = []
    for name, ws_v, rest_v, exact in fields:
        classification = classify_price(ws_v, rest_v, tick_size=None, exact=exact)
        out.append(
            OracleComparison(
                window=window,
                field=name,
                ws_value=ws_v,
                rest_value=rest_v,
                tick_size=None,
                classification=classification,
                method=_METHOD_BY_CLASS[classification.value],
            )
        )
    return tuple(out)


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + fsync + rename), mode 0600."""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
