"""Unit tests for compacted reference recovery (DECOUPLING PHASE D).

Covers extraction (non-fabricating), non-destructive monotonic merge, writer outcomes/counters
against an in-memory store reusing the real merge/ordering logic, and loader validation/gating.
Real Redis atomicity (WATCH/MULTI), TTL, trading-date isolation, and restart recovery are in the
integration suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.market_ipc import (
    CompactedReferenceState,
    MarketEventEnvelope,
    ReferenceOutcome,
    ReferenceSnapshot,
    ReferenceStateLoader,
    ReferenceStateWriter,
    build_envelope,
    merge_reference,
    reference_from_envelope,
)
from app.market_ipc.reference import _classify
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    ProviderSessionOhlc,
    Quote,
    Tick,
)

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _ohlc(
    open_: str = "3400", high: str = "3450", low: str = "3390", close: str = "3420"
) -> ProviderSessionOhlc:
    return ProviderSessionOhlc(
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
    )


def _tick(*, ohlc: ProviderSessionOhlc | None = None, price: str = "3410") -> Tick:
    return Tick(
        instrument=_instrument(), event_timestamp=_NOW, last_price=Decimal(price), session_ohlc=ohlc
    )


def _reference(previous_close: str = "3395.50") -> MarketReference:
    return MarketReference(instrument=_instrument(), previous_close=Decimal(previous_close))


def _envelope(
    payload: object,
    *,
    seq: int,
    epoch: int = 1,
    producer: str = _PRODUCER,
    uv: int = 7,
    td: date = _TD,
) -> MarketEventEnvelope:
    return build_envelope(
        payload,  # type: ignore[arg-type]
        producer_id=producer,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=td,
        universe_version=uv,
    )


# --------------------------------------------------------------------------- #
# extraction — never fabricates
# --------------------------------------------------------------------------- #
def test_market_reference_extracts_previous_close_only() -> None:
    state = reference_from_envelope(_envelope(_reference("3395.50"), seq=1))
    assert state is not None
    assert state.previous_close == Decimal("3395.50")
    assert state.session_open is None  # not fabricated


def test_tick_session_ohlc_extracts_session_fields_only() -> None:
    state = reference_from_envelope(_envelope(_tick(ohlc=_ohlc()), seq=1))
    assert state is not None
    assert state.session_open == Decimal("3400")
    assert state.session_high == Decimal("3450")
    assert state.session_low == Decimal("3390")
    assert state.session_close == Decimal("3420")
    assert state.previous_close is None  # not fabricated


def test_tick_without_session_ohlc_yields_no_reference() -> None:
    assert reference_from_envelope(_envelope(_tick(ohlc=None), seq=1)) is None


def test_quote_and_feed_continuity_yield_no_reference() -> None:
    quote = Quote(
        instrument=_instrument(),
        event_timestamp=_NOW,
        bid_price=Decimal("1"),
        ask_price=Decimal("2"),
        bid_quantity=1,
        ask_quantity=2,
    )
    assert reference_from_envelope(_envelope(quote, seq=1)) is None
    continuity = FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_NOW)
    assert reference_from_envelope(_envelope(continuity, seq=1)) is None


# --------------------------------------------------------------------------- #
# non-destructive merge
# --------------------------------------------------------------------------- #
def test_merge_is_non_destructive_across_reference_then_tick() -> None:
    ref = reference_from_envelope(_envelope(_reference("100"), seq=1))
    tick = reference_from_envelope(_envelope(_tick(ohlc=_ohlc("102", "105", "101", "104")), seq=2))
    assert ref is not None and tick is not None
    merged = merge_reference(ref, tick)
    assert merged.previous_close == Decimal("100")  # not cleared by the later tick
    assert merged.session_open == Decimal("102")
    assert merged.producer_sequence == 2  # provenance advances to the newer event


def test_merge_later_reference_keeps_session_open() -> None:
    tick = reference_from_envelope(_envelope(_tick(ohlc=_ohlc("102", "105", "101", "104")), seq=1))
    ref = reference_from_envelope(_envelope(_reference("100"), seq=2))
    assert tick is not None and ref is not None
    merged = merge_reference(tick, ref)
    assert merged.session_open == Decimal("102")  # not cleared by the later reference
    assert merged.previous_close == Decimal("100")


def test_classify_ordering() -> None:
    first = reference_from_envelope(_envelope(_reference("100"), seq=5, epoch=1))
    newer = reference_from_envelope(_envelope(_reference("101"), seq=6, epoch=1))
    older = reference_from_envelope(_envelope(_reference("99"), seq=4, epoch=1))
    dup = reference_from_envelope(_envelope(_reference("100"), seq=5, epoch=1))
    assert _classify(first, None) is ReferenceOutcome.WRITTEN
    assert _classify(newer, first) is ReferenceOutcome.MERGED
    assert _classify(older, first) is ReferenceOutcome.STALE_REJECTED
    assert _classify(dup, first) is ReferenceOutcome.DUPLICATE


def test_new_epoch_is_newer_than_prior_epoch() -> None:
    prior = reference_from_envelope(_envelope(_reference("100"), seq=999, epoch=1))
    restart = reference_from_envelope(_envelope(_reference("101"), seq=1, epoch=2))
    assert _classify(restart, prior) is ReferenceOutcome.MERGED  # epoch dominates sequence


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #
def test_state_serialization_preserves_decimal_and_awareness() -> None:
    state = reference_from_envelope(_envelope(_reference("3410.25"), seq=1))
    assert state is not None
    restored = CompactedReferenceState.model_validate_json(state.model_dump_json())
    assert restored.previous_close == Decimal("3410.25")
    assert restored.updated_at.tzinfo is not None


# --------------------------------------------------------------------------- #
# writer (against an in-memory store reusing the real merge/ordering logic)
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple[date, str], CompactedReferenceState] = {}
        self.fail = False

    async def compact(self, incoming: CompactedReferenceState) -> ReferenceOutcome:
        if self.fail:
            from redis.exceptions import RedisError

            raise RedisError("down")
        key = (incoming.trading_date, incoming.instrument_identity)
        existing = self.data.get(key)
        outcome = _classify(incoming, existing)
        if outcome in (ReferenceOutcome.DUPLICATE, ReferenceOutcome.STALE_REJECTED):
            return outcome
        self.data[key] = merge_reference(existing, incoming) if existing else incoming
        return outcome


def _writer(store: _FakeStore) -> ReferenceStateWriter:
    return ReferenceStateWriter(store=store, now=lambda: _NOW)  # type: ignore[arg-type]


async def test_writer_first_write_then_merge_then_stale_then_duplicate() -> None:
    store = _FakeStore()
    writer = _writer(store)
    assert await writer.update(_envelope(_reference("100"), seq=1)) is ReferenceOutcome.WRITTEN
    assert await writer.update(_envelope(_tick(ohlc=_ohlc()), seq=2)) is ReferenceOutcome.MERGED
    # same ordering (epoch 1, seq 2) as stored -> duplicate; older seq -> stale
    assert await writer.update(_envelope(_reference("99"), seq=2)) is ReferenceOutcome.DUPLICATE
    assert (
        await writer.update(_envelope(_reference("98"), seq=1)) is ReferenceOutcome.STALE_REJECTED
    )
    diag = writer.diagnostics()
    assert diag.reference_updates_written == 2
    assert diag.previous_close_updates == 1
    assert diag.session_open_updates == 1
    assert diag.duplicate_updates == 1
    assert diag.stale_updates_rejected == 1


async def test_writer_no_reference_data_for_bare_tick() -> None:
    store = _FakeStore()
    writer = _writer(store)
    assert (
        await writer.update(_envelope(_tick(ohlc=None), seq=1))
        is ReferenceOutcome.NO_REFERENCE_DATA
    )
    assert writer.diagnostics().no_reference_data == 1
    assert writer.diagnostics().reference_updates_written == 0


async def test_writer_redis_failure_is_isolated() -> None:
    store = _FakeStore()
    store.fail = True
    writer = _writer(store)
    assert await writer.update(_envelope(_reference("100"), seq=1)) is ReferenceOutcome.WRITE_FAILED
    assert writer.diagnostics().reference_update_failures == 1
    assert writer.diagnostics().last_failure_at is not None


# --------------------------------------------------------------------------- #
# loader
# --------------------------------------------------------------------------- #
class _FakeSource:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self._entries = entries

    async def read_all_raw(self, trading_date: date) -> dict[str, bytes]:
        return dict(self._entries)


def _raw(state: CompactedReferenceState) -> bytes:
    return state.model_dump_json().encode("utf-8")


def _loader(entries: dict[str, bytes]) -> ReferenceStateLoader:
    return ReferenceStateLoader(source=_FakeSource(entries), now=lambda: _NOW)


async def test_loader_empty_key_is_warming_up() -> None:
    snapshot = await _loader({}).load(_TD, expected_universe_version=7)
    assert isinstance(snapshot, ReferenceSnapshot)
    assert snapshot.warming_up
    assert snapshot.diagnostics.entries_total == 0


async def test_loader_loads_valid_and_isolates_malformed() -> None:
    good = reference_from_envelope(_envelope(_reference("100"), seq=1))
    assert good is not None
    entries = {"NSE:TCS": _raw(good), "NSE:BAD": b"not-json"}
    snapshot = await _loader(entries).load(_TD, expected_universe_version=7)
    assert set(snapshot.states) == {"NSE:TCS"}
    assert snapshot.diagnostics.entries_loaded == 1
    assert snapshot.diagnostics.entries_invalid == 1
    assert snapshot.diagnostics.previous_close_count == 1


async def test_loader_rejects_wrong_trading_date_and_universe() -> None:
    wrong_day = reference_from_envelope(_envelope(_reference("100"), seq=1, td=date(2026, 9, 8)))
    wrong_uv = reference_from_envelope(_envelope(_reference("100"), seq=2, uv=6))
    assert wrong_day is not None and wrong_uv is not None
    entries = {"NSE:A": _raw(wrong_day), "NSE:B": _raw(wrong_uv)}
    snapshot = await _loader(entries).load(_TD, expected_universe_version=7)
    assert snapshot.states == {}
    assert snapshot.diagnostics.trading_date_mismatch_count == 1
    assert snapshot.diagnostics.universe_mismatch_count == 1


async def test_loader_counts_complete_reference() -> None:
    ref = reference_from_envelope(_envelope(_reference("100"), seq=1))
    tick = reference_from_envelope(_envelope(_tick(ohlc=_ohlc()), seq=2))
    assert ref is not None and tick is not None
    complete = merge_reference(ref, tick)  # has both previous_close and session_open
    snapshot = await _loader({"NSE:TCS": _raw(complete)}).load(_TD, expected_universe_version=7)
    assert snapshot.diagnostics.complete_reference_count == 1
    assert snapshot.states["NSE:TCS"].previous_close == Decimal("100")


async def test_loader_unknown_universe_when_no_authority() -> None:
    ref = reference_from_envelope(_envelope(_reference("100"), seq=1))
    assert ref is not None
    snapshot = await _loader({"NSE:TCS": _raw(ref)}).load(_TD, expected_universe_version=None)
    assert snapshot.states == {}
    assert snapshot.diagnostics.universe_mismatch_count == 1  # UNKNOWN is not silently accepted
