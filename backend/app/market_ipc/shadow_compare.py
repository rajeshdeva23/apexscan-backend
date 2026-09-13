"""Offline canonical-event parity comparator for the shadow IPC path (DECOUPLING PHASE H4C).

Compares the canonical events a fixture *expected* against the canonical events the shadow
consumer actually *applied*, and classifies each logical event: MATCH, VALUE_MISMATCH,
MISSING, UNEXPECTED, DUPLICATE_SUPPRESSED, DECODE_FAILURE, UNSUPPORTED, or
KNOWN_B2_DUPLICATE. It is a pure, deterministic function over canonical values — it holds no
Redis client, spawns no task, contacts no provider, and never touches the production
TickEngine / MarketContext / strategies.

Design invariants (mirrors the H4B guarantees):

* **Semantic, not serialization.** Events are compared by canonical model equality
  (Decimals exact, timestamps tz-aware) and field diffs come from ``model_dump`` — never from
  raw JSON bytes, so formatting noise can never look like drift.
* **Identity, not order.** Parity is keyed on the ``(producer_id, producer_epoch,
  producer_sequence)`` dedup identity, so a new epoch reusing a sequence is a distinct event
  and reclaim/new-read interleaving (H4B: no global order) never causes a false mismatch.
  ``ORDERING_MODEL`` is ``identity_set``.
* **No loss inference from gaps.** Only events explicitly present in the fixture are expected;
  a legal producer-sequence gap is never reported as MISSING (no sequence arithmetic).
* **C1 duplicates are healthy.** A fixture identity that appears more than once but is applied
  once is ``DUPLICATE_SUPPRESSED`` (not MISSING/UNEXPECTED). An identity applied *more* than
  once is the ``KNOWN_B2_DUPLICATE`` apply->mark window — surfaced, never hidden, never solved.
* **Bounded output.** Totals are scalars; the mismatch sample is capped by ``sample_limit``.

This proves the offline framework only. Live provider-timestamp parity remains gated on the
FIX-2 track (RC3 INCONCLUSIVE): fixtures carry already-correct tz-aware timestamps and this
module makes no live-timestamp claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.market_ipc.envelope import MarketEventEnvelope, ProducerEventIdentity
from app.market_ipc.events import EventKind, IpcPayload, decode_payload

ORDERING_MODEL = "identity_set"
_MAX_VALUE_CHARS = 200  # bound a single field value in the mismatch sample


class ParityClass(StrEnum):
    """Deterministic classification of one logical event's parity outcome."""

    MATCH = "match"
    VALUE_MISMATCH = "value_mismatch"
    MISSING = "missing"
    UNEXPECTED = "unexpected"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    DECODE_FAILURE = "decode_failure"
    UNSUPPORTED = "unsupported"
    KNOWN_B2_DUPLICATE = "known_b2_duplicate"  # apply->mark window reapply (H8A), never hidden


@dataclass(frozen=True, slots=True)
class CanonicalEventView:
    """One canonical event reduced to its dedup identity, kind, and payload for comparison."""

    identity: ProducerEventIdentity
    kind: EventKind
    payload: IpcPayload

    @classmethod
    def from_pair(cls, envelope: MarketEventEnvelope, payload: IpcPayload) -> CanonicalEventView:
        """Build a view from an applied ``(envelope, payload)`` pair (as the shadow sink keeps)."""
        return cls(
            identity=ProducerEventIdentity.from_envelope(envelope),
            kind=envelope.event_kind,
            payload=payload,
        )


class ParityMismatch(BaseModel):
    """One bounded, serializable mismatch record (no secrets, no unbounded payloads)."""

    model_config = ConfigDict(frozen=True)

    classification: ParityClass
    identity: str | None
    kind: str | None
    field: str | None = None
    expected: str | None = None
    actual: str | None = None


class ParityReport(BaseModel):
    """Bounded, serializable parity result for one replay (diagnostic-safe)."""

    model_config = ConfigDict(frozen=True)

    ordering_model: str
    expected_total: int
    actual_total: int
    matched_total: int
    missing_total: int
    unexpected_total: int
    value_mismatch_total: int
    duplicate_suppressed_total: int
    decode_failure_total: int
    unsupported_total: int
    known_b2_duplicate_total: int
    sample: tuple[ParityMismatch, ...]

    @property
    def is_clean(self) -> bool:
        """True when parity is perfect: no anomaly of any kind (suppressed dups are healthy)."""
        return (
            self.missing_total == 0
            and self.unexpected_total == 0
            and self.value_mismatch_total == 0
            and self.decode_failure_total == 0
            and self.unsupported_total == 0
            and self.known_b2_duplicate_total == 0
        )


def view_from_envelope(envelope: MarketEventEnvelope) -> CanonicalEventView:
    """Decode an envelope's payload into a comparison view (used to build expected fixtures)."""
    return CanonicalEventView.from_pair(
        envelope, decode_payload(envelope.event_kind, envelope.payload)
    )


def views_from_applied(
    applied: Iterable[tuple[MarketEventEnvelope, IpcPayload]],
) -> list[CanonicalEventView]:
    """Reduce a shadow sink's retained ``(envelope, payload)`` pairs to comparison views."""
    return [CanonicalEventView.from_pair(envelope, payload) for envelope, payload in applied]


def compare(
    expected: Sequence[CanonicalEventView],
    actual: Sequence[CanonicalEventView],
    *,
    decode_failures: int = 0,
    unsupported: int = 0,
    sample_limit: int = 50,
) -> ParityReport:
    """Classify actual applied events against expected fixture events by dedup identity.

    ``decode_failures`` / ``unsupported`` are aggregate counts sourced from the consumer's
    diagnostics (poison entries carry no recoverable identity, so they are surfaced as totals,
    never silently dropped). ``sample_limit`` bounds the retained mismatch detail.
    """
    expected_first, expected_counts = _index(expected)
    actual_first, actual_counts = _index(actual)
    tally = _Tally(sample_limit)
    for identity, exp in expected_first.items():
        _tally_expected(
            tally,
            identity,
            exp,
            expected_counts[identity],
            actual_counts.get(identity, 0),
            actual_first,
        )
    for identity, act in actual_first.items():
        if identity not in expected_first:
            tally.unexpected += actual_counts[identity]
            tally.add(_mismatch(ParityClass.UNEXPECTED, identity, act.kind))
    return ParityReport(
        ordering_model=ORDERING_MODEL,
        expected_total=len(expected),
        actual_total=len(actual),
        matched_total=tally.matched,
        missing_total=tally.missing,
        unexpected_total=tally.unexpected,
        value_mismatch_total=tally.value_mismatch,
        duplicate_suppressed_total=tally.dup_suppressed,
        decode_failure_total=decode_failures,
        unsupported_total=unsupported,
        known_b2_duplicate_total=tally.b2,
        sample=tuple(tally.sample),
    )


class _Tally:
    """Mutable accumulator for one comparison pass (bounded sample)."""

    def __init__(self, sample_limit: int) -> None:
        self.matched = 0
        self.missing = 0
        self.unexpected = 0
        self.value_mismatch = 0
        self.dup_suppressed = 0
        self.b2 = 0
        self.sample: list[ParityMismatch] = []
        self._limit = sample_limit

    def add(self, mismatch: ParityMismatch) -> None:
        """Append a mismatch while the bounded sample has room (totals are counted regardless)."""
        if len(self.sample) < self._limit:
            self.sample.append(mismatch)


def _index(
    views: Sequence[CanonicalEventView],
) -> tuple[dict[ProducerEventIdentity, CanonicalEventView], dict[ProducerEventIdentity, int]]:
    """Return (first-occurrence-by-identity, occurrence-count-by-identity), insertion-ordered."""
    first: dict[ProducerEventIdentity, CanonicalEventView] = {}
    counts: dict[ProducerEventIdentity, int] = {}
    for view in views:
        counts[view.identity] = counts.get(view.identity, 0) + 1
        first.setdefault(view.identity, view)
    return first, counts


def _tally_expected(
    tally: _Tally,
    identity: ProducerEventIdentity,
    exp: CanonicalEventView,
    exp_count: int,
    act_count: int,
    actual_first: dict[ProducerEventIdentity, CanonicalEventView],
) -> None:
    """Classify one expected identity against what was actually applied."""
    if act_count == 0:
        tally.missing += 1
        tally.add(_mismatch(ParityClass.MISSING, identity, exp.kind))
        return
    act = actual_first[identity]
    if exp.kind == act.kind and exp.payload == act.payload:
        tally.matched += 1
    else:
        name, expected_value, actual_value = _field_diff(exp, act)
        tally.value_mismatch += 1
        tally.add(
            _mismatch(
                ParityClass.VALUE_MISMATCH, identity, exp.kind, name, expected_value, actual_value
            )
        )
    if exp_count > act_count:  # fixture repeated this identity beyond what applied: C1 suppressed
        tally.dup_suppressed += exp_count - act_count
    if act_count > 1:  # the sink applied it more than once: the B2 apply->mark window
        tally.b2 += act_count - 1
        tally.add(_mismatch(ParityClass.KNOWN_B2_DUPLICATE, identity, exp.kind))


def _field_diff(exp: CanonicalEventView, act: CanonicalEventView) -> tuple[str, str, str]:
    """Return the first differing (field, expected, actual) between two views, bounded."""
    if exp.kind != act.kind:
        return "event_kind", exp.kind.value, act.kind.value
    expected_dump = exp.payload.model_dump()
    actual_dump = act.payload.model_dump()
    for name in sorted(set(expected_dump) | set(actual_dump)):
        if expected_dump.get(name) != actual_dump.get(name):
            return name, _bounded(expected_dump.get(name)), _bounded(actual_dump.get(name))
    return "unknown", "", ""  # defensive: equality already returned False above


def _bounded(value: object) -> str:
    """Stringify a field value, truncating so the sample can never grow unbounded per entry."""
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else f"{text[: _MAX_VALUE_CHARS - 3]}..."


def _mismatch(
    classification: ParityClass,
    identity: ProducerEventIdentity,
    kind: EventKind,
    field: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
) -> ParityMismatch:
    """Build a bounded mismatch record with a serializable identity string."""
    return ParityMismatch(
        classification=classification,
        identity=f"{identity.producer_id}:{identity.producer_epoch}:{identity.producer_sequence}",
        kind=kind.value,
        field=field,
        expected=expected,
        actual=actual,
    )
