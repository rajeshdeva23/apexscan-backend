"""Generic, timeframe-agnostic live candle aggregation (docs/06 §13; ADR-005, ADR-006).

Accepted, ordered canonical ticks (validated upstream by P4.2/P4.3) are aggregated
into per-``instrument × timeframe`` candles. The algorithm is generic over any
valid timeframe. Buckets are session-relative (anchored at the P4.3
``regular_open``).

Per ADR-006, a snapshot feed cannot prove exact boundary OHLCV, so a finalized
live interval is **never** authoritative here: it is retained as an immutable
``IncompleteCandle`` (awaiting P4.5 reconciliation), carrying provisional OHLC and
— only for a contiguous, uninterrupted interval — a provisional volume delta. A
non-contiguous (gap) interval or one overlapping a feed-continuity loss carries no
volume and invalidates the carried baseline; the baseline is never rolled across a
gap. The ``COMPLETE → canonical Candle`` path exists but is reachable only via
reconciliation, not from live snapshots. No strategy or provider knowledge, no
historical fetching.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.market_engine.buckets import bucket_bounds
from app.market_engine.context import (
    CandleQuality,
    IncompleteCandle,
    MarketState,
    PartialCandle,
    SessionContext,
    TimeframeCandles,
)
from app.market_engine.historical.reconciliation import (
    CandleIdentity,
    ReconciliationOutcome,
    ReconciliationResult,
    identity_of,
    identity_of_incomplete,
    match_outcome,
)
from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, FeedContinuity, FeedContinuityEvent, Instrument, Tick

_DEFAULT_FINALIZED_WINDOW = 20
_CONTINUITY_LOSS = frozenset({FeedContinuity.DISCONNECTED, FeedContinuity.CONTINUITY_LOST})


@dataclass(slots=True)
class _Bucket:
    """Mutable in-progress aggregation state for one candle bucket (engine-internal)."""

    index: int
    start_utc: datetime
    end_utc: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    baseline: int | None
    last_cumulative: int | None
    feed_gap: bool = False
    ohlc_broken: bool = False
    volume_invalid: bool = False


@dataclass(slots=True)
class _TimeframeState:
    """Per-``instrument × timeframe`` engine state (bounded, in memory only)."""

    instrument: Instrument
    timeframe: Timeframe
    finalized: deque[Candle]
    incomplete: deque[IncompleteCandle]
    trading_date: date | None = None
    partial: _Bucket | None = None
    previous_bucket_index: int | None = None
    previous_bucket_end_cumulative: int | None = None


class CandleEngine:
    """Aggregates accepted ticks into per-instrument, per-timeframe candle facts."""

    def __init__(
        self,
        *,
        schedule: SessionSchedule,
        exchange_timezone: str,
        timeframes: Iterable[Timeframe],
        finalized_window: int = _DEFAULT_FINALIZED_WINDOW,
    ) -> None:
        """Configure the engine with a session schedule, timezone, and timeframes.

        Args:
            schedule: The P4.3 session boundaries (bucket anchor and truncation).
            exchange_timezone: IANA timezone for exchange-local bucket alignment.
            timeframes: Required timeframes; duplicates are deduplicated.
            finalized_window: Max recent finalized/incomplete candles per stream.

        Raises:
            ValueError: If ``finalized_window`` is not positive or an intraday
                timeframe is not shorter than the session.
        """
        if finalized_window <= 0:
            raise ValueError("finalized_window must be a positive integer")
        self._schedule = schedule
        self._timezone = ZoneInfo(exchange_timezone)
        self._finalized_window = finalized_window
        session_length = self._session_length()
        for timeframe in timeframes:
            if timeframe.duration is not None and timeframe.duration >= session_length:
                raise ValueError("an intraday timeframe must be shorter than the session")
        self._timeframes = tuple(sorted(set(timeframes), key=lambda tf: (tf.is_session, tf.label)))
        self._states: dict[tuple[Instrument, Timeframe], _TimeframeState] = {}
        self._continuity_broken: set[Instrument] = set()

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        """Return the registered timeframes in deterministic order."""
        return self._timeframes

    def update(self, tick: Tick, session: SessionContext) -> None:
        """Aggregate one accepted tick into every registered timeframe.

        Only ticks in the continuous live session are aggregated (docs/06 §13.2).

        Args:
            tick: An accepted canonical tick (ordered/validated upstream).
            session: The session facts stamped for this tick (P4.3).
        """
        if session.market_state is not MarketState.LIVE_SESSION:
            return
        for timeframe in self._timeframes:
            self._update_timeframe(tick, session, timeframe)

    def record_continuity(self, event: FeedContinuityEvent) -> None:
        """Apply a feed-wide continuity fact to all instruments being aggregated.

        A continuity loss taints every active in-progress candle (OHLC no longer
        trustworthy) and invalidates carried volume baselines so later buckets
        cannot inherit pre-gap state. Reconnection does not restore completeness
        (ADR-006 §11); only a new trading session clears the taint.

        Args:
            event: The broker-neutral continuity fact from the Data Provider.
        """
        if event.status not in _CONTINUITY_LOSS:
            return
        for (instrument, _timeframe), state in self._states.items():
            self._continuity_broken.add(instrument)
            if state.partial is not None:
                state.partial.ohlc_broken = True

    def flush(self, at_time: datetime) -> None:
        """Finalize every partial whose bucket has closed at or before ``at_time``.

        Deterministic and caller-driven — no scheduler or wall-clock polling.
        Empty intervals are never fabricated; finalized facts are never mutated.

        Args:
            at_time: The deterministic instant up to which buckets are closed.
        """
        for state in self._states.values():
            if state.partial is not None and state.partial.end_utc <= at_time:
                self._finalize_partial(state)

    def reconcile(self, authoritative: Candle, timeframe: Timeframe) -> ReconciliationResult:
        """Replace a matching incomplete interval with an authoritative finalized candle.

        Exact identity (instrument, timeframe, start, end) is required. The original
        incomplete interval is never mutated — it is removed and the authoritative
        candle is inserted into the bounded, chronological finalized window. The
        operation is idempotent and never touches the active partial candle.

        Args:
            authoritative: The authoritative canonical candle to install.
            timeframe: The timeframe the candle belongs to.

        Returns:
            A :class:`ReconciliationResult` describing the outcome (state changes
            only on ``RECONCILED``).
        """
        identity = identity_of(authoritative, timeframe)
        state = self._states.get((authoritative.instrument, timeframe))
        if state is None:
            return ReconciliationResult(identity=identity, outcome=ReconciliationOutcome.NO_MATCH)
        outcome = match_outcome(
            authoritative=authoritative,
            timeframe=timeframe,
            incomplete=state.incomplete,
            finalized=state.finalized,
        )
        if outcome is ReconciliationOutcome.RECONCILED:
            self._apply_reconciliation(state, authoritative, timeframe, identity)
            return ReconciliationResult(identity=identity, outcome=outcome, candle=authoritative)
        if outcome is ReconciliationOutcome.NO_MATCH and self._is_out_of_window(
            state, authoritative
        ):
            return ReconciliationResult(
                identity=identity, outcome=ReconciliationOutcome.OUT_OF_WINDOW
            )
        candle = authoritative if outcome is ReconciliationOutcome.ALREADY_RECONCILED else None
        return ReconciliationResult(identity=identity, outcome=outcome, candle=candle)

    def _apply_reconciliation(
        self,
        state: _TimeframeState,
        authoritative: Candle,
        timeframe: Timeframe,
        identity: CandleIdentity,
    ) -> None:
        state.incomplete = deque(
            (item for item in state.incomplete if identity_of_incomplete(item) != identity),
            maxlen=self._finalized_window,
        )
        kept = [item for item in state.finalized if identity_of(item, timeframe) != identity]
        kept.append(authoritative)
        kept.sort(key=lambda candle: candle.start_timestamp)
        state.finalized = deque(kept[-self._finalized_window :], maxlen=self._finalized_window)

    @staticmethod
    def _is_out_of_window(state: _TimeframeState, authoritative: Candle) -> bool:
        retained = [item.start_timestamp for item in state.incomplete]
        retained += [item.start_timestamp for item in state.finalized]
        if state.partial is not None:
            retained.append(state.partial.start_utc)
        if not retained:
            return False
        return authoritative.start_timestamp < min(retained)

    def candle_sets_for(self, instrument: Instrument) -> tuple[TimeframeCandles, ...]:
        """Return an immutable per-timeframe candle snapshot for an instrument."""
        sets: list[TimeframeCandles] = []
        for timeframe in self._timeframes:
            state = self._states.get((instrument, timeframe))
            partial = self._partial_view(state.partial) if state and state.partial else None
            finalized = tuple(state.finalized) if state is not None else ()
            incomplete = tuple(state.incomplete) if state is not None else ()
            sets.append(
                TimeframeCandles(
                    timeframe=timeframe,
                    partial=partial,
                    finalized=finalized,
                    incomplete=incomplete,
                )
            )
        return tuple(sets)

    def _update_timeframe(self, tick: Tick, session: SessionContext, timeframe: Timeframe) -> None:
        state = self._state_for(tick.instrument, timeframe)
        if state.trading_date != session.trading_date:
            self._reset_session(state, session.trading_date)
        index, start_utc, end_utc = self._bucket_bounds(
            tick.event_timestamp, session.trading_date, timeframe
        )
        if state.partial is not None and state.partial.index == index:
            self._apply_tick(state.partial, tick)
            return
        if state.partial is not None:
            self._finalize_partial(state)
        state.partial = self._open_bucket(state, index, start_utc, end_utc, tick)

    def _state_for(self, instrument: Instrument, timeframe: Timeframe) -> _TimeframeState:
        key = (instrument, timeframe)
        state = self._states.get(key)
        if state is None:
            state = _TimeframeState(
                instrument=instrument,
                timeframe=timeframe,
                finalized=deque(maxlen=self._finalized_window),
                incomplete=deque(maxlen=self._finalized_window),
            )
            self._states[key] = state
        return state

    def _reset_session(self, state: _TimeframeState, trading_date: date) -> None:
        if state.partial is not None:
            self._finalize_partial(state)
        state.trading_date = trading_date
        state.partial = None
        state.previous_bucket_index = None
        state.previous_bucket_end_cumulative = None
        self._continuity_broken.discard(state.instrument)

    def _open_bucket(
        self,
        state: _TimeframeState,
        index: int,
        start_utc: datetime,
        end_utc: datetime,
        tick: Tick,
    ) -> _Bucket:
        contiguous = (
            state.previous_bucket_index is not None and index == state.previous_bucket_index + 1
        )
        broken = state.instrument in self._continuity_broken
        feed_gap = broken or (state.previous_bucket_index is not None and not contiguous)
        baseline = None if feed_gap or not contiguous else state.previous_bucket_end_cumulative
        price = tick.last_price
        return _Bucket(
            index=index,
            start_utc=start_utc,
            end_utc=end_utc,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            baseline=baseline,
            last_cumulative=tick.session_cumulative_volume,
            feed_gap=feed_gap,
        )

    def _apply_tick(self, bucket: _Bucket, tick: Tick) -> None:
        price = tick.last_price
        bucket.high_price = max(bucket.high_price, price)
        bucket.low_price = min(bucket.low_price, price)
        bucket.close_price = price
        cumulative = tick.session_cumulative_volume
        if cumulative is None:
            return
        if bucket.last_cumulative is not None and cumulative < bucket.last_cumulative:
            bucket.volume_invalid = True
            return
        bucket.last_cumulative = cumulative

    def _finalize_partial(self, state: _TimeframeState) -> None:
        bucket = state.partial
        if bucket is None:
            return
        state.incomplete.append(self._to_incomplete(state.instrument, state.timeframe, bucket))
        state.previous_bucket_index = bucket.index
        if bucket.feed_gap or bucket.last_cumulative is None:
            state.previous_bucket_end_cumulative = None
        else:
            state.previous_bucket_end_cumulative = bucket.last_cumulative
        state.partial = None

    @staticmethod
    def _quality_for(bucket: _Bucket) -> CandleQuality:
        """Classify a finalized live bucket; never ``COMPLETE`` from live snapshots."""
        if bucket.feed_gap:
            return CandleQuality.FEED_GAP
        if bucket.ohlc_broken:
            return CandleQuality.INCOMPLETE_OHLC
        return CandleQuality.INCOMPLETE_VOLUME

    def _to_incomplete(
        self, instrument: Instrument, timeframe: Timeframe, bucket: _Bucket
    ) -> IncompleteCandle:
        return IncompleteCandle(
            instrument=instrument,
            timeframe=timeframe,
            start_timestamp=bucket.start_utc,
            end_timestamp=bucket.end_utc,
            open_price=bucket.open_price,
            high_price=bucket.high_price,
            low_price=bucket.low_price,
            close_price=bucket.close_price,
            traded_quantity=self._interval_volume(bucket),
            quality=self._quality_for(bucket),
        )

    def _partial_view(self, bucket: _Bucket) -> PartialCandle:
        return PartialCandle(
            start_timestamp=bucket.start_utc,
            end_timestamp=bucket.end_utc,
            open_price=bucket.open_price,
            high_price=bucket.high_price,
            low_price=bucket.low_price,
            close_price=bucket.close_price,
            traded_quantity=self._interval_volume(bucket),
            quality=self._quality_for(bucket),
        )

    @staticmethod
    def _interval_volume(bucket: _Bucket) -> int | None:
        """Return the provisional boundary-delta volume, or None when unavailable."""
        if bucket.volume_invalid or bucket.baseline is None or bucket.last_cumulative is None:
            return None
        volume = bucket.last_cumulative - bucket.baseline
        if volume < 0:
            return None
        return volume

    def _local(self, trading_date: date, moment: time) -> datetime:
        """Combine a trading date and an exchange-local time into an aware datetime."""
        return datetime.combine(trading_date, moment, tzinfo=self._timezone)

    def _bucket_bounds(
        self, event_timestamp: datetime, trading_date: date, timeframe: Timeframe
    ) -> tuple[int, datetime, datetime]:
        return bucket_bounds(
            event_timestamp=event_timestamp,
            trading_date=trading_date,
            timeframe=timeframe,
            schedule=self._schedule,
            timezone=self._timezone,
        )

    def _session_length(self) -> timedelta:
        anchor = date(2000, 1, 1)
        open_local = self._local(anchor, self._schedule.regular_open)
        close_local = self._local(anchor, self._schedule.regular_close)
        return close_local - open_local
