"""Bounded observation-state ordering rules for the sector shadow runtime (SECTOR-VIEW-1B)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.sector_intelligence.state import (
    LatestObservation,
    ObservationState,
    RecordOutcome,
)

_TD = date(2026, 9, 3)
_NEXT = date(2026, 9, 4)
_TS = datetime(2026, 9, 3, 6, 30, tzinfo=UTC)
_EXPECTED = frozenset({"NSE:A", "NSE:B", "NSE:C"})


def _obs(
    identity: str = "NSE:A",
    *,
    ts: datetime = _TS,
    td: date | None = _TD,
    last: Decimal | None = Decimal("105"),
    prev: Decimal | None = Decimal("100"),
    open_: Decimal | None = Decimal("101"),
    version: int = 1,
) -> LatestObservation:
    return LatestObservation(
        identity=identity,
        trading_date=td,
        observation_timestamp=ts,
        last_price=last,
        previous_close=prev,
        session_open=open_,
        version=version,
    )


def _state() -> ObservationState:
    return ObservationState(_EXPECTED)


def test_first_observation_is_accepted_and_sets_the_session() -> None:
    state = _state()
    assert state.record(_obs()) is RecordOutcome.ACCEPTED
    assert len(state) == 1
    assert state.trading_date == _TD


def test_unknown_identity_is_rejected_and_never_stored() -> None:
    state = _state()
    assert state.record(_obs("NSE:UNKNOWN")) is RecordOutcome.REJECTED_UNKNOWN
    assert len(state) == 0


def test_equal_timestamp_is_an_idempotent_duplicate() -> None:
    state = _state()
    state.record(_obs(last=Decimal("105")))
    assert state.record(_obs(last=Decimal("106"))) is RecordOutcome.DUPLICATE
    assert len(state) == 1  # no second entry
    assert state.coherent_copy()[0].last_price == Decimal("106")  # replaced in place


def test_older_timestamp_is_out_of_order_and_does_not_rewind() -> None:
    state = _state()
    state.record(_obs(ts=_TS + timedelta(seconds=10), last=Decimal("110")))
    outcome = state.record(_obs(ts=_TS, last=Decimal("100")))
    assert outcome is RecordOutcome.REJECTED_OUT_OF_ORDER
    assert state.coherent_copy()[0].last_price == Decimal("110")  # newer stays authoritative


def test_forward_trading_date_rolls_and_clears_prior_session() -> None:
    state = _state()
    state.record(_obs("NSE:A"))
    state.record(_obs("NSE:B"))
    assert len(state) == 2
    outcome = state.record(_obs("NSE:C", td=_NEXT, ts=_TS + timedelta(days=1)))
    assert outcome is RecordOutcome.ROLLED
    assert state.trading_date == _NEXT
    assert len(state) == 1  # prior-session state cleared
    assert state.coherent_copy()[0].identity == "NSE:C"


def test_backward_trading_date_is_a_late_prior_session_event_and_rejected() -> None:
    state = _state()
    state.record(_obs("NSE:A", td=_NEXT, ts=_TS + timedelta(days=1)))
    outcome = state.record(_obs("NSE:B", td=_TD, ts=_TS))
    assert outcome is RecordOutcome.REJECTED_LATE_DATE
    assert len(state) == 1


def test_state_cardinality_never_exceeds_expected_universe() -> None:
    state = _state()
    for i in range(1000):  # far more events than the 3-identity universe
        identity = f"NSE:{'ABC'[i % 3]}"
        state.record(_obs(identity, ts=_TS + timedelta(seconds=i)))
    assert len(state) <= len(_EXPECTED)
    assert len(state) == 3


def test_none_trading_date_is_stored_but_leaves_session_unchanged() -> None:
    state = _state()
    state.record(_obs("NSE:A"))
    assert state.record(_obs("NSE:B", td=None)) is RecordOutcome.ACCEPTED
    assert state.trading_date == _TD  # unchanged by the dateless observation


def test_coherent_copy_is_an_immutable_snapshot() -> None:
    state = _state()
    state.record(_obs("NSE:A"))
    snapshot = state.coherent_copy()
    state.record(_obs("NSE:B"))
    assert len(snapshot) == 1  # the earlier copy is not mutated by later records
    assert isinstance(snapshot, tuple)
