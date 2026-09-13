"""Unit proofs for the H4C offline parity comparator (DECOUPLING PHASE H4C).

No Redis: the comparator is a pure, deterministic function over canonical events. These cover
the classification contract (match/value-mismatch/missing/unexpected/duplicate-suppressed/
known-B2/decode-failure/unsupported), identity-set (order-insensitive) semantics, new-epoch
distinctness, legal-gap tolerance, bounded sampling, determinism, serializability, and the
FIX-2 timestamp guard. The full publish->consume->compare replay lives in the integration suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.market_ipc import (
    ORDERING_MODEL,
    ParityClass,
    ParityReport,
    build_envelope,
    compare,
    view_from_envelope,
)
from app.market_ipc.events import decode_payload
from app.schemas.market_data import Instrument, Quote, Tick

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"


def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(price),
    )


def _env(*, seq: int, epoch: int = 1, symbol: str = "TCS", price: str = "100.5") -> object:
    return build_envelope(
        _tick(symbol, price),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _view(**kwargs: object) -> object:
    return view_from_envelope(_env(**kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# T01: exact semantic match, order-insensitive (H4B: no global order guarantee)
# --------------------------------------------------------------------------- #
def test_exact_semantic_match_is_order_insensitive() -> None:
    expected = [_view(seq=s) for s in (1, 2, 3)]
    applied = [_view(seq=s) for s in (3, 1, 2)]  # applied in a different order
    report = compare(expected, applied)
    assert report.is_clean
    assert report.matched_total == 3
    assert report.ordering_model == ORDERING_MODEL == "identity_set"
    assert report.sample == ()


# --------------------------------------------------------------------------- #
# T02: a differing field is a VALUE_MISMATCH with field/expected/actual detail
# --------------------------------------------------------------------------- #
def test_value_mismatch_reports_field_expected_actual() -> None:
    report = compare([_view(seq=1, price="100.5")], [_view(seq=1, price="250.0")])
    assert report.value_mismatch_total == 1
    assert not report.is_clean
    (mismatch,) = report.sample
    assert mismatch.classification is ParityClass.VALUE_MISMATCH
    assert mismatch.field == "last_price"
    assert mismatch.expected == "100.5"
    assert mismatch.actual == "250.0"
    assert mismatch.identity == "market-ingestion:1:1"


def test_event_kind_mismatch_is_reported_on_the_kind() -> None:
    quote = build_envelope(
        Quote(
            instrument=Instrument(exchange="NSE", symbol="TCS"),
            event_timestamp=_NOW,
            bid_price=Decimal("100"),
            ask_price=Decimal("101"),
            bid_quantity=1,
            ask_quantity=1,
        ),
        producer_id=_PRODUCER,
        producer_epoch=1,
        producer_sequence=1,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )
    report = compare([_view(seq=1)], [view_from_envelope(quote)])
    assert report.value_mismatch_total == 1
    assert report.sample[0].field == "event_kind"
    assert report.sample[0].expected == "tick"
    assert report.sample[0].actual == "quote"


# --------------------------------------------------------------------------- #
# T03 / T04: missing vs unexpected are distinct
# --------------------------------------------------------------------------- #
def test_missing_event_is_classified_missing() -> None:
    report = compare([_view(seq=1), _view(seq=2)], [_view(seq=1)])
    assert report.missing_total == 1
    assert report.unexpected_total == 0
    assert report.sample[0].classification is ParityClass.MISSING
    assert report.sample[0].identity == "market-ingestion:1:2"


def test_unexpected_event_is_classified_unexpected() -> None:
    report = compare([_view(seq=1)], [_view(seq=1), _view(seq=2)])
    assert report.unexpected_total == 1
    assert report.missing_total == 0
    assert report.sample[0].classification is ParityClass.UNEXPECTED
    assert report.sample[0].identity == "market-ingestion:1:2"


# --------------------------------------------------------------------------- #
# T05: C1 duplicate suppression is healthy, never MISSING/UNEXPECTED
# --------------------------------------------------------------------------- #
def test_duplicate_suppression_is_healthy() -> None:
    expected = [_view(seq=1), _view(seq=1), _view(seq=2)]  # fixture repeats identity 1
    applied = [_view(seq=1), _view(seq=2)]  # C1 suppressed the second seq-1
    report = compare(expected, applied)
    assert report.duplicate_suppressed_total == 1
    assert report.matched_total == 2
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert report.is_clean


# --------------------------------------------------------------------------- #
# T06: same sequence under a new epoch is a distinct event, not a duplicate
# --------------------------------------------------------------------------- #
def test_same_sequence_new_epoch_is_distinct() -> None:
    expected = [_view(seq=1, epoch=10), _view(seq=1, epoch=11)]
    report = compare(expected, list(expected))
    assert report.matched_total == 2
    assert report.duplicate_suppressed_total == 0
    assert report.is_clean


# --------------------------------------------------------------------------- #
# T07: legal producer-sequence gaps are never inferred as loss
# --------------------------------------------------------------------------- #
def test_legal_sequence_gaps_are_not_missing() -> None:
    expected = [_view(seq=100), _view(seq=102)]  # gap at 101: legal, not loss
    report = compare(expected, [_view(seq=102), _view(seq=100)])
    assert report.is_clean
    assert report.matched_total == 2
    assert report.missing_total == 0


# --------------------------------------------------------------------------- #
# T10: the B2 apply->mark reapply window is surfaced separately, never hidden
# --------------------------------------------------------------------------- #
def test_b2_duplicate_application_is_surfaced_separately() -> None:
    report = compare([_view(seq=1)], [_view(seq=1), _view(seq=1)])  # applied twice
    assert report.known_b2_duplicate_total == 1
    assert report.matched_total == 1
    assert not report.is_clean
    b2 = [m for m in report.sample if m.classification is ParityClass.KNOWN_B2_DUPLICATE]
    assert len(b2) == 1
    assert b2[0].identity == "market-ingestion:1:1"


# --------------------------------------------------------------------------- #
# T11 / UNSUPPORTED: poison and unsupported events surface as totals, not MISSING
# --------------------------------------------------------------------------- #
def test_decode_failures_surface_as_total_not_missing() -> None:
    report = compare([_view(seq=1)], [_view(seq=1)], decode_failures=2)
    assert report.decode_failure_total == 2
    assert report.matched_total == 1
    assert report.missing_total == 0  # poison carries no identity -> counted, never faked missing
    assert not report.is_clean


def test_unsupported_surfaces_as_total() -> None:
    report = compare([_view(seq=1)], [_view(seq=1)], unsupported=1)
    assert report.unsupported_total == 1
    assert not report.is_clean


# --------------------------------------------------------------------------- #
# T13: determinism — identical inputs produce an identical report
# --------------------------------------------------------------------------- #
def test_comparison_is_deterministic() -> None:
    expected = [_view(seq=s) for s in range(1, 20)]
    applied = [_view(seq=s, price="1.0") for s in range(5, 25)]  # mismatches + missing + unexpected
    assert compare(expected, applied).model_dump() == compare(expected, applied).model_dump()


# --------------------------------------------------------------------------- #
# T14: the mismatch sample is bounded while totals stay complete
# --------------------------------------------------------------------------- #
def test_mismatch_sample_is_bounded() -> None:
    expected = [_view(seq=s) for s in range(1, 201)]  # 200 all-missing events
    report = compare(expected, [], sample_limit=10)
    assert report.missing_total == 200
    assert len(report.sample) == 10


# --------------------------------------------------------------------------- #
# T22 / T23: fixtures use canonical tz-aware UTC time; no +5:30/-5:30 workaround
# --------------------------------------------------------------------------- #
def test_fixture_timestamps_are_canonical_utc() -> None:
    envelope = _env(seq=1)
    assert envelope.produced_at.tzinfo is not None  # type: ignore[attr-defined]
    assert envelope.produced_at.utcoffset() == timedelta(0)  # type: ignore[attr-defined]
    payload = decode_payload(envelope.event_kind, envelope.payload)  # type: ignore[attr-defined]
    assert payload.event_timestamp.tzinfo is not None  # type: ignore[union-attr]
    assert payload.event_timestamp.utcoffset() == timedelta(0)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# T25: report + sample round-trip through JSON (diagnostic-safe)
# --------------------------------------------------------------------------- #
def test_report_is_serializable() -> None:
    report = compare([_view(seq=1)], [_view(seq=1, price="9.9")])
    restored = ParityReport.model_validate_json(report.model_dump_json())
    assert restored == report
    assert restored.sample[0].classification is ParityClass.VALUE_MISMATCH


def test_empty_replay_is_clean() -> None:
    report = compare([], [])
    assert report.is_clean
    assert report.expected_total == 0
    assert report.actual_total == 0
