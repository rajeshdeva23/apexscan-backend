"""Immutable rollback planning for production promotion (DEPLOY-1).

Rollback means re-promoting the PREVIOUS known-good immutable artifact — never a
source rebuild, ``git revert``, or ``:latest``. If the previous artifact cannot
be identified, deployment must not proceed: there is nothing to roll back to.
Because a rollback restarts the backend and backend startup can authenticate
Dhan, a rollback that would restart is gated on the same explicit operator
confirmation; without it we surface operator intervention instead of looping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

_INVALID_REFS = frozenset({"", "latest", "unknown", "none", "null"})


class RollbackDecision(StrEnum):
    """Outcome of planning a rollback."""

    READY = "ready"
    UNAVAILABLE = "unavailable"
    REQUIRES_OPERATOR_INTERVENTION = "requires_operator_intervention"


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """A planned rollback.

    Attributes:
        decision: Whether rollback can proceed automatically.
        target_ref: The immutable artifact to re-promote, or None.
        reason: Explanation of the decision.
    """

    decision: RollbackDecision
    target_ref: str | None
    reason: str


def is_immutable_ref(ref: str | None) -> bool:
    """Whether ``ref`` names a specific immutable artifact (not latest/unknown)."""
    if ref is None:
        return False
    normalized = ref.strip().lower()
    if normalized in _INVALID_REFS:
        return False
    return not normalized.endswith(":latest")


def plan_rollback(
    *,
    previous_artifact: str | None,
    restart_required: bool,
    dhan_restart_safety_confirmed: bool,
) -> RollbackPlan:
    """Plan a rollback to the previous immutable artifact, failing closed.

    Args:
        previous_artifact: The digest/SHA-pinned reference that was running before
            this deploy, or None/latest/unknown if it could not be established.
        restart_required: Whether applying the rollback restarts the backend.
        dhan_restart_safety_confirmed: Operator confirmation that a backend
            restart will not trip the Dhan token rate-limit hazard.

    Returns:
        A :class:`RollbackPlan`. UNAVAILABLE when no immutable target exists;
        REQUIRES_OPERATOR_INTERVENTION when a restart is needed but Dhan restart
        safety is unconfirmed; READY otherwise.
    """
    if not is_immutable_ref(previous_artifact):
        return RollbackPlan(
            RollbackDecision.UNAVAILABLE,
            None,
            "no previous immutable artifact recorded; cannot roll back",
        )
    if restart_required and not dhan_restart_safety_confirmed:
        return RollbackPlan(
            RollbackDecision.REQUIRES_OPERATOR_INTERVENTION,
            previous_artifact,
            "rollback restarts the backend but Dhan restart safety is unconfirmed",
        )
    return RollbackPlan(RollbackDecision.READY, previous_artifact, "previous artifact available")
