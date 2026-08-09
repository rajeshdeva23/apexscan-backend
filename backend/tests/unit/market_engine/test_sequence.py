"""Tests for the deterministic, replay-safe sequence abstraction (docs/06 §12.2)."""

from __future__ import annotations

from app.market_engine.sequence import MonotonicSequence


def test_sequence_starts_at_one_and_increments_without_skipping() -> None:
    sequence = MonotonicSequence()
    assert [sequence.next_value() for _ in range(5)] == [1, 2, 3, 4, 5]


def test_sequence_current_reflects_last_issued_value() -> None:
    sequence = MonotonicSequence()
    assert sequence.current == 0
    sequence.next_value()
    sequence.next_value()
    assert sequence.current == 2


def test_fresh_sequences_replay_identically() -> None:
    first = [MonotonicSequence().next_value() for _ in range(3)]
    second = [MonotonicSequence().next_value() for _ in range(3)]
    assert first == second == [1, 1, 1]

    run_a = MonotonicSequence()
    run_b = MonotonicSequence()
    assert [run_a.next_value() for _ in range(4)] == [run_b.next_value() for _ in range(4)]


def test_sequences_are_independent_not_global() -> None:
    a = MonotonicSequence()
    b = MonotonicSequence()
    a.next_value()
    a.next_value()
    assert b.next_value() == 1
    assert a.current == 2
    assert b.current == 1
