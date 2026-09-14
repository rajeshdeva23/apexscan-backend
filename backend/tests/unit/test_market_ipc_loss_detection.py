"""B11 loss-detection reconciliation — the pure classifier (PHASE H8C).

Covers the reconcile() taxonomy deterministically with no Redis. The real-Redis metadata reading
and injected reset/rewind/tail-loss scenarios live in the integration suite. Proves the hard rules:
never infer loss from sequence arithmetic (a legal producer-sequence gap is healthy), scope every
decision to one producer incarnation (a new epoch is not a rewind), and fail closed.
"""

from __future__ import annotations

from app.market_ipc.continuity import (
    ContinuityReason,
    ContinuityState,
    FeedContinuityTracker,
)
from app.market_ipc.envelope import ProducerEventIdentity
from app.market_ipc.loss_detection import (
    ConsumerProgressEvidence,
    LossDetectionState,
    ProducerPublicationEvidence,
    StreamMetadata,
    reconcile,
)

_PRODUCER = "market-ingestion"


def _producer(
    *,
    epoch: int = 1,
    last_published: int | None = 10,
    terminal: bool = False,
    uncertain: bool = False,
) -> ProducerPublicationEvidence:
    return ProducerPublicationEvidence(
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        last_published_sequence=last_published,
        terminal_publication_break=terminal,
        publication_outcome_uncertain=uncertain,
    )


def _meta(
    *,
    epoch: int = 1,
    last_seq: int | None = 10,
    length: int = 10,
    last_generated_id: str = "1000-0",
    group_exists: bool = True,
    group_last_delivered_id: str = "1000-0",
    pending: int = 0,
) -> StreamMetadata:
    identity = ProducerEventIdentity(_PRODUCER, epoch, last_seq) if last_seq is not None else None
    return StreamMetadata(
        stream_exists=True,
        length=length,
        last_generated_id=last_generated_id,
        group_exists=group_exists,
        group_last_delivered_id=group_last_delivered_id,
        pending=pending,
        last_entry_identity=identity,
    )


_CAUGHT_UP = ConsumerProgressEvidence(1, 10)
_BEHIND = ConsumerProgressEvidence(1, 3)
_NONE = ConsumerProgressEvidence()


# T01 healthy
def test_h8c_t01_healthy_when_caught_up() -> None:
    result = reconcile(_producer(), _CAUGHT_UP, _meta())
    assert result.state is LossDetectionState.HEALTHY
    assert result.ready_for_authority is True


# T02 consumer lagging
def test_h8c_t02_consumer_lagging_is_ready() -> None:
    result = reconcile(_producer(), _BEHIND, _meta())
    assert result.state is LossDetectionState.CONSUMER_LAGGING
    assert result.ready_for_authority is True


# T03 pending recovery
def test_h8c_t03_pending_recovery_is_ready() -> None:
    result = reconcile(_producer(), _BEHIND, _meta(pending=2))
    assert result.state is LossDetectionState.PENDING_RECOVERY
    assert result.ready_for_authority is True


# T04 legitimate retention (applied then trimmed)
def test_h8c_t04_retention_expected_when_applied_then_trimmed() -> None:
    result = reconcile(_producer(), _CAUGHT_UP, _meta(length=0, last_seq=None))
    assert result.state is LossDetectionState.RETENTION_EXPECTED
    assert result.ready_for_authority is True


# T05 redis reset — stream absent / never written
def test_h8c_t05_reset_when_stream_absent() -> None:
    absent = StreamMetadata(False, 0, "0-0", False, "0-0", 0, None)
    result = reconcile(_producer(), _NONE, absent)
    assert result.state is LossDetectionState.REDIS_STREAM_RESET
    assert result.ready_for_authority is False


def test_h8c_t05_reset_when_group_absent_under_live_producer() -> None:
    result = reconcile(_producer(), _NONE, _meta(group_exists=False))
    assert result.state is LossDetectionState.REDIS_STREAM_RESET
    assert result.ready_for_authority is False


# T06 rewind — group delivered beyond the stream's last id
def test_h8c_t06_rewind_when_group_ahead_of_stream() -> None:
    result = reconcile(
        _producer(), _CAUGHT_UP, _meta(group_last_delivered_id="2000-0", last_generated_id="1500-0")
    )
    assert result.state is LossDetectionState.REDIS_STATE_REWIND
    assert result.ready_for_authority is False


# T07 producer publication failure is distinguished from Redis loss
def test_h8c_t07_producer_failure_not_redis_loss() -> None:
    result = reconcile(_producer(terminal=True), _NONE, _meta())
    assert result.state is LossDetectionState.PRODUCER_PUBLICATION_FAILED
    assert result.ready_for_authority is False


# T08 a legal producer-sequence gap is NOT loss (mandatory)
def test_h8c_t08_legal_sequence_gap_is_not_loss() -> None:
    # Producer published up to seq 102 (101 was legally skipped); the stream's last entry is 102.
    # The detector compares the published position to the last entry — it never looks for "101".
    result = reconcile(
        _producer(last_published=102),
        ConsumerProgressEvidence(1, 102),
        _meta(last_seq=102),
    )
    assert result.state is LossDetectionState.HEALTHY
    assert result.ready_for_authority is True


# T09 a new producer epoch is NOT a rewind (sequence reset is legal)
def test_h8c_t09_new_epoch_is_not_rewind() -> None:
    result = reconcile(
        _producer(epoch=2, last_published=3),
        ConsumerProgressEvidence(2, 3),
        _meta(epoch=2, last_seq=3),
    )
    assert result.state is LossDetectionState.HEALTHY
    assert result.ready_for_authority is True


# T11 metadata-derived: an uncertain producer outcome fails closed
def test_h8c_t11_uncertain_outcome_fails_closed() -> None:
    result = reconcile(_producer(uncertain=True), _NONE, _meta())
    assert result.state is LossDetectionState.INSUFFICIENT_EVIDENCE
    assert result.ready_for_authority is False


# tail-loss: the confirmed tail is gone from the stream
def test_h8c_tail_loss_is_unaccounted_for() -> None:
    result = reconcile(_producer(last_published=10), _BEHIND, _meta(last_seq=7))
    assert result.state is LossDetectionState.PUBLISHED_EVENT_UNACCOUNTED_FOR
    assert result.ready_for_authority is False


# trimmed before applied (un-accounted) fails closed rather than assuming legitimate retention
def test_h8c_trimmed_before_applied_is_unaccounted_for() -> None:
    result = reconcile(_producer(), _BEHIND, _meta(length=0, last_seq=None))
    assert result.state is LossDetectionState.PUBLISHED_EVENT_UNACCOUNTED_FOR
    assert result.ready_for_authority is False


# nothing published yet is trivially healthy
def test_h8c_nothing_published_is_healthy() -> None:
    result = reconcile(_producer(last_published=None), _NONE, _meta(last_seq=None, length=0))
    assert result.state is LossDetectionState.HEALTHY


# stream tail from a different incarnation is not reconcilable here (fail closed)
def test_h8c_mismatched_incarnation_tail_is_insufficient() -> None:
    result = reconcile(_producer(epoch=2), _NONE, _meta(epoch=1, last_seq=10))
    assert result.state is LossDetectionState.INSUFFICIENT_EVIDENCE
    assert result.ready_for_authority is False


# from_continuity projects the L1 snapshot correctly (accepted vs published; terminal; uncertain)
def test_h8c_from_continuity_uses_published_position_and_terminal_flags() -> None:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id=_PRODUCER, producer_epoch=4)
    tracker.publication_accepted(producer_sequence=9)  # accepted but NOT published
    tracker.publication_succeeded(producer_sequence=7)  # last D1-confirmed publish
    evidence = ProducerPublicationEvidence.from_continuity(tracker.snapshot())
    assert evidence.producer_epoch == 4
    assert evidence.last_published_sequence == 7  # published, never the accepted 9
    assert evidence.terminal_publication_break is False

    tracker.publication_uncertain()
    uncertain = ProducerPublicationEvidence.from_continuity(tracker.snapshot())
    assert uncertain.publication_outcome_uncertain is True
    assert uncertain.terminal_publication_break is True  # uncertain is a terminal BROKEN state
    # reconcile must fail closed on the uncertain outcome (checked before the terminal branch)
    assert (
        reconcile(uncertain, _NONE, _meta(epoch=4, last_seq=7)).state
        is LossDetectionState.INSUFFICIENT_EVIDENCE
    )


# a definite terminal break (not uncertain) is producer failure
def test_h8c_from_continuity_terminal_break_is_producer_failure() -> None:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id=_PRODUCER, producer_epoch=1)
    tracker.publication_succeeded(producer_sequence=5)
    tracker.publication_failed()  # definite terminal break
    assert tracker.snapshot().state is ContinuityState.BROKEN
    assert tracker.snapshot().reason is ContinuityReason.PUBLICATION_FAILED
    evidence = ProducerPublicationEvidence.from_continuity(tracker.snapshot())
    assert reconcile(evidence, _NONE, _meta(last_seq=5)).state is (
        LossDetectionState.PRODUCER_PUBLICATION_FAILED
    )
