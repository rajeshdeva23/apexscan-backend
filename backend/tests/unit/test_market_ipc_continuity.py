"""Unit tests for the broker-neutral feed-continuity runtime (DECOUPLING L1).

Covers the §40 matrix: initial state, incarnation identity, accepted-vs-published positions,
legal sequence gaps (never provider-loss), explicit overflow / worker-failure / publication
failure / unknown-outcome terminal breaks with stickiness, provider disconnect/reconnect and
recovery evidence, clean vs incomplete shutdown, new-epoch reset, bounded snapshot, hot-path
purity, the M2/D1 bridges, and a credential audit.
"""

from __future__ import annotations

from app.market_ipc import (
    BoundaryDiagnostics,
    BoundaryState,
    ContinuityReason,
    ContinuityState,
    DrainResult,
    FeedContinuityTracker,
    PublishOutcome,
    SubmitOutcome,
)


def _started(producer_id: str = "market-ingestion", epoch: int = 7) -> FeedContinuityTracker:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id=producer_id, producer_epoch=epoch)
    return tracker


def _boundary_diagnostics(**overrides: object) -> BoundaryDiagnostics:
    defaults: dict[str, object] = dict(
        state=BoundaryState.RUNNING,
        queue_depth=0,
        queue_capacity=10,
        queue_high_watermark=0,
        enqueued_total=0,
        published_total=0,
        publish_failure_total=0,
        overflow_total=0,
        prepare_rejected_total=0,
        worker_running=True,
        last_failure=None,
        last_publish_success_at=None,
        last_publish_failure_at=None,
    )
    defaults.update(overrides)
    return BoundaryDiagnostics(**defaults)  # type: ignore[arg-type]


# 1. initial continuity state
def test_initial_state_is_not_started() -> None:
    snapshot = FeedContinuityTracker().snapshot()
    assert snapshot.state is ContinuityState.NOT_STARTED
    assert snapshot.reason is ContinuityReason.NONE
    assert snapshot.producer_id is None


# 2 + 3. producer startup + incarnation identity
def test_producer_started_is_healthy_with_identity() -> None:
    snapshot = _started("mi", 7).snapshot()
    assert snapshot.state is ContinuityState.HEALTHY
    assert (snapshot.producer_id, snapshot.producer_epoch) == ("mi", 7)


# 4 + 5. accepted / published progression
def test_accepted_and_published_positions_advance() -> None:
    tracker = _started()
    tracker.publication_accepted(producer_sequence=100)
    tracker.publication_succeeded(producer_sequence=100)
    snapshot = tracker.snapshot()
    assert snapshot.last_accepted_sequence == 100
    assert snapshot.last_published_sequence == 100


# 35. accepted != published (enqueue is not Redis commit)
def test_accepted_leads_published_while_worker_blocked() -> None:
    tracker = _started()
    tracker.publication_accepted(producer_sequence=5)
    blocked = tracker.snapshot()
    assert blocked.last_accepted_sequence == 5
    assert blocked.last_published_sequence is None  # enqueued, not yet published
    tracker.publication_succeeded(producer_sequence=5)
    assert tracker.snapshot().last_published_sequence == 5


# 6 + 31. legal sequence gap is NOT provider loss
def test_legal_sequence_gap_does_not_break_continuity() -> None:
    tracker = _started()
    for seq in (100, 101, 103, 104):  # 102 never observed (e.g. legally rejected elsewhere)
        tracker.publication_accepted(producer_sequence=seq)
        tracker.publication_succeeded(producer_sequence=seq)
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.HEALTHY  # arithmetic gap is not loss evidence
    assert snapshot.last_published_sequence == 104


# 7 + 8 + 32. explicit overflow is terminal and sticky
def test_overflow_is_terminal_and_does_not_auto_clear() -> None:
    tracker = _started()
    tracker.publication_accepted(producer_sequence=100)
    tracker.publication_overflow()
    assert tracker.state is ContinuityState.BROKEN
    assert tracker.snapshot().reason is ContinuityReason.PUBLICATION_QUEUE_OVERFLOW
    tracker.publication_succeeded(producer_sequence=101)  # a later success must NOT clear it
    assert tracker.state is ContinuityState.BROKEN
    assert tracker.snapshot().continuity_break_total == 1


# 9 + 33. worker failure terminal; reconnect never clears
def test_worker_failure_is_terminal_and_reconnect_does_not_clear() -> None:
    tracker = _started()
    tracker.worker_failed()
    assert tracker.state is ContinuityState.BROKEN
    assert tracker.snapshot().reason is ContinuityReason.PUBLICATION_WORKER_FAILED
    tracker.provider_disconnected()
    tracker.provider_connected()
    tracker.publication_succeeded(producer_sequence=1)
    assert tracker.state is ContinuityState.BROKEN  # still broken for this incarnation


# 10. definite publication failure is terminal
def test_publication_failure_is_terminal() -> None:
    tracker = _started()
    tracker.publication_failed(producer_sequence=100)
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.BROKEN
    assert snapshot.reason is ContinuityReason.PUBLICATION_FAILED
    assert snapshot.publication_failure_total == 1


# 11 + 36. unknown outcome: terminal, never claims loss or success
def test_unknown_outcome_is_terminal_without_asserting_loss_or_success() -> None:
    tracker = _started()
    tracker.publication_accepted(producer_sequence=100)
    tracker.publication_uncertain(producer_sequence=100)
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.BROKEN
    assert snapshot.reason is ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN
    assert snapshot.last_published_sequence is None  # never claimed published
    assert snapshot.publication_uncertain_total == 1


# 12. provider disconnect is recoverable degradation
def test_provider_disconnect_is_recoverable() -> None:
    tracker = _started()
    tracker.provider_connected()
    tracker.provider_disconnected()
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.PROVIDER_DEGRADED
    assert snapshot.reason is ContinuityReason.PROVIDER_DISCONNECTED
    assert snapshot.provider_connected is False


# 13 + 15 + 16(recovery). reconnect alone is not healthy; a publish is the evidence
def test_reconnect_requires_publication_evidence_to_recover() -> None:
    tracker = _started()
    tracker.provider_connected()
    tracker.provider_disconnected()
    tracker.provider_connected()  # socket back, but not yet proven flowing
    awaiting = tracker.snapshot()
    assert awaiting.state is ContinuityState.PROVIDER_DEGRADED
    assert awaiting.reason is ContinuityReason.AWAITING_RECOVERY_EVIDENCE
    tracker.publication_succeeded(producer_sequence=1)  # recovery evidence
    assert tracker.state is ContinuityState.HEALTHY


# 14 + 34. provider reconnect within a process keeps the same epoch (idempotent start)
def test_repeat_producer_started_same_epoch_does_not_reset() -> None:
    tracker = _started("mi", 7)
    tracker.publication_succeeded(producer_sequence=50)
    tracker.producer_started(producer_id="mi", producer_epoch=7)  # ordinary reconnect
    snapshot = tracker.snapshot()
    assert snapshot.producer_epoch == 7
    assert snapshot.last_published_sequence == 50  # state preserved, not reset


# 18 + 38. a new epoch is an independent incarnation that resets a terminal break
def test_new_epoch_resets_terminal_break() -> None:
    tracker = _started("mi", 7)
    tracker.publication_overflow()
    assert tracker.state is ContinuityState.BROKEN
    tracker.producer_started(producer_id="mi", producer_epoch=8)
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.HEALTHY
    assert snapshot.producer_epoch == 8
    assert snapshot.reason is ContinuityReason.NONE
    assert snapshot.continuity_break_total == 1  # cumulative history retained (bounded)


# 16. clean shutdown
def test_clean_shutdown_reports_stopped_clean() -> None:
    tracker = _started()
    tracker.publication_succeeded(producer_sequence=1)
    tracker.begin_shutdown()
    tracker.drain_completed(DrainResult(drained_complete=True, pending_at_stop=0))
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.STOPPED
    assert snapshot.reason is ContinuityReason.CLEAN_SHUTDOWN
    assert snapshot.incomplete_drain is False


# 17. incomplete drain is visible
def test_incomplete_drain_is_visible() -> None:
    tracker = _started()
    tracker.begin_shutdown()
    tracker.drain_completed(DrainResult(drained_complete=False, pending_at_stop=3))
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.STOPPED
    assert snapshot.reason is ContinuityReason.INCOMPLETE_DRAIN
    assert snapshot.incomplete_drain is True
    assert snapshot.pending_at_stop == 3


def test_clean_drain_after_break_keeps_break_reason() -> None:
    tracker = _started()
    tracker.worker_failed()
    tracker.drain_completed(DrainResult(drained_complete=True, pending_at_stop=0))
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.STOPPED
    assert snapshot.reason is ContinuityReason.PUBLICATION_WORKER_FAILED  # break not hidden


# 19. bounded snapshot exposes the documented fields
def test_snapshot_is_bounded_and_complete() -> None:
    tracker = _started()
    tracker.observe_boundary(_boundary_diagnostics(queue_depth=2, queue_capacity=10))
    snapshot = tracker.snapshot()
    assert snapshot.queue_capacity == 10
    assert snapshot.queue_depth == 2
    assert snapshot.worker_running is True


# 27. no credentials/secrets in the snapshot
def test_snapshot_carries_no_credentials() -> None:
    tracker = _started("market-ingestion", 7)
    tracker.publication_failed(producer_sequence=1)
    text = repr(tracker.snapshot()).lower()
    for secret in ("token", "password", "pin", "totp", "secret", "key", "@"):
        assert secret not in text


# ------------------------------------------------------------------- #
# M2 / D1 bridges
# ------------------------------------------------------------------- #
def test_record_submission_maps_m2_outcomes() -> None:
    tracker = _started()
    tracker.record_submission(SubmitOutcome.ENQUEUED, producer_sequence=1)
    assert tracker.snapshot().last_accepted_sequence == 1
    tracker.record_submission(SubmitOutcome.FAILED_OVERSIZE)  # data-validity, non-terminal
    assert tracker.state is ContinuityState.HEALTHY
    assert tracker.snapshot().prepare_rejected_total == 1
    tracker.record_submission(SubmitOutcome.REJECTED_OVERFLOW)  # terminal
    assert tracker.state is ContinuityState.BROKEN


def test_record_submission_not_running_is_ignored() -> None:
    tracker = _started()
    tracker.record_submission(SubmitOutcome.REJECTED_NOT_RUNNING)
    assert tracker.state is ContinuityState.HEALTHY  # lifecycle, not a continuity break


def test_record_publication_maps_transport_failure_conservatively() -> None:
    tracker = _started()
    tracker.record_publication(PublishOutcome.PUBLISHED, producer_sequence=1)
    assert tracker.snapshot().last_published_sequence == 1
    tracker.record_publication(PublishOutcome.FAILED_TRANSPORT, producer_sequence=2)
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.BROKEN  # conservative: transport failure is a break
    assert snapshot.reason is ContinuityReason.PUBLICATION_FAILED


# 23 + 20. boundary bridge derives terminal worker failure; deltas are idempotent
def test_observe_boundary_derives_worker_failure_and_is_idempotent() -> None:
    tracker = _started()
    failed = _boundary_diagnostics(state=BoundaryState.FAILED, worker_running=False)
    tracker.observe_boundary(failed)
    tracker.observe_boundary(failed)  # repeated snapshot must not double-count
    snapshot = tracker.snapshot()
    assert snapshot.state is ContinuityState.BROKEN
    assert snapshot.reason is ContinuityReason.PUBLICATION_WORKER_FAILED
    assert snapshot.continuity_break_total == 1


def test_observe_boundary_derives_overflow_break_from_counter_delta() -> None:
    tracker = _started()
    tracker.observe_boundary(_boundary_diagnostics(overflow_total=0))
    tracker.observe_boundary(_boundary_diagnostics(overflow_total=1))
    assert tracker.state is ContinuityState.BROKEN
    assert tracker.snapshot().reason is ContinuityReason.PUBLICATION_QUEUE_OVERFLOW
