"""Immutable rollback planning (DEPLOY-1)."""

from __future__ import annotations

import pytest

from deploy.rollback import RollbackDecision, is_immutable_ref, plan_rollback

_DIGEST = "ghcr.io/o/apexscan-backend@sha256:" + "a" * 64


def test_ready_when_previous_artifact_and_dhan_confirmed() -> None:
    plan = plan_rollback(
        previous_artifact=_DIGEST, restart_required=True, dhan_restart_safety_confirmed=True
    )
    assert plan.decision is RollbackDecision.READY
    assert plan.target_ref == _DIGEST


def test_ready_when_no_restart_required() -> None:
    plan = plan_rollback(
        previous_artifact=_DIGEST, restart_required=False, dhan_restart_safety_confirmed=False
    )
    assert plan.decision is RollbackDecision.READY


@pytest.mark.parametrize(
    "bad", [None, "", "latest", "unknown", "ghcr.io/o/apexscan-backend:latest"]
)
def test_unavailable_without_immutable_target(bad: str | None) -> None:
    plan = plan_rollback(
        previous_artifact=bad, restart_required=True, dhan_restart_safety_confirmed=True
    )
    assert plan.decision is RollbackDecision.UNAVAILABLE
    assert plan.target_ref is None


def test_requires_operator_when_restart_not_dhan_safe() -> None:
    plan = plan_rollback(
        previous_artifact=_DIGEST, restart_required=True, dhan_restart_safety_confirmed=False
    )
    assert plan.decision is RollbackDecision.REQUIRES_OPERATOR_INTERVENTION
    assert plan.target_ref == _DIGEST


def test_is_immutable_ref() -> None:
    assert is_immutable_ref(_DIGEST)
    assert is_immutable_ref("ghcr.io/o/apexscan-backend:" + "a" * 40)
    assert not is_immutable_ref("ghcr.io/o/apexscan-backend:latest")
    assert not is_immutable_ref(None)
    assert not is_immutable_ref("  UNKNOWN  ")
