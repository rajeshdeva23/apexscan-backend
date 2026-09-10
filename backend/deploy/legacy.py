"""One-time legacy → SHA-pinned rollback bridge (DEPLOY-3A / B1).

The first pipeline deployment runs over a legacy backend that reports no runtime
``build_sha`` and whose running image has no pullable GHCR digest. A
:class:`LegacyRollbackArtifact` pins the EXACT running image — by a digest
published from the running image id, never a rebuild (source-commit equality is
not image equality) — plus an explicit, clearly-marked *provenance-only* source
SHA that is never treated as runtime-attested.

The exception is narrowly scoped so it cannot become the normal path
(:func:`select_rollback_target`): a legacy artifact is consulted ONLY when the
running backend reports no ``build_sha``. Once a DEPLOY-1+ image is running
(``build_sha`` present), normal rollback semantics are mandatory and the legacy
artifact is ignored even if supplied. Legacy rollback is verified by
:func:`legacy_rollback_verified` (running digest + health/startup/readiness, no
SHA) and reported distinctly, never as a SHA-verified rollback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_DIGEST_REF = re.compile(r"^([a-z0-9][a-z0-9./_-]*/)?apexscan-backend@sha256:[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class SourceShaEvidenceKind(StrEnum):
    """Where a legacy artifact's provenance-only source SHA came from."""

    IMAGE_TAG = "image_tag"  # git SHA read from the running image's tag
    DEPLOY_RECORD = "deploy_record"  # a recorded prior deployment
    OPERATOR_ASSERTED = "operator_asserted"


class LegacyArtifactError(ValueError):
    """Raised when a legacy rollback artifact is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class LegacyRollbackArtifact:
    """An immutable, one-time rollback target preserving the running legacy image.

    Attributes:
        image_digest: Pullable GHCR digest published FROM the running image id.
        running_image_id: The local ``sha256:...`` id the digest was derived from.
        source_sha_evidence: Provenance-only git SHA (NOT runtime-attested).
        source_sha_evidence_kind: How that SHA was established.
        provenance: Free-text description of how the artifact was produced.
        legacy_unverified_build_sha: Always True — marks the SHA as unattested.
    """

    image_digest: str
    running_image_id: str
    source_sha_evidence: str
    source_sha_evidence_kind: SourceShaEvidenceKind
    provenance: str
    legacy_unverified_build_sha: bool = True

    def __post_init__(self) -> None:
        """Reject a malformed or non-legacy artifact, failing fast."""
        if not _DIGEST_REF.match(self.image_digest):
            raise LegacyArtifactError("image_digest must be a pullable apexscan-backend digest")
        if not _IMAGE_ID.match(self.running_image_id):
            raise LegacyArtifactError("running_image_id must be sha256:<64 hex>")
        if not _FULL_SHA.match(self.source_sha_evidence):
            raise LegacyArtifactError("source_sha_evidence must be a full git SHA")
        if self.legacy_unverified_build_sha is not True:
            raise LegacyArtifactError("legacy_unverified_build_sha must be True")


@dataclass(frozen=True, slots=True)
class RollbackTarget:
    """A resolved rollback target and whether the legacy exception was used."""

    source_sha: str
    image_digest: str
    legacy: bool


def select_rollback_target(
    *,
    runtime_build_sha: str | None,
    running_digest: str | None,
    legacy: LegacyRollbackArtifact | None,
    version_responded: bool = True,
) -> RollbackTarget | None:
    """Resolve the rollback target, enforcing the legacy-exception scope.

    Normal path (mandatory whenever the backend attests a ``build_sha``): the
    running image must itself be an immutable digest. The legacy artifact is
    consulted ONLY when the version endpoint *responded* but carried no
    ``build_sha`` (a genuine pre-DEPLOY-1 image). A broken/unreachable/malformed
    ``/version`` (``version_responded=False``) fails closed and never re-enters
    legacy mode, so a transient endpoint failure on a modern image cannot cause a
    rollback to the ancient legacy image.

    Args:
        runtime_build_sha: ``/version.build_sha`` of the running backend, or None.
        running_digest: The running image's immutable digest, if any.
        legacy: A provisioned legacy artifact, or None.
        version_responded: Whether ``/version`` returned a valid response at all.

    Returns:
        A :class:`RollbackTarget`, or None if no safe target exists.
    """
    if not version_responded:
        return None  # unreadable /version -> fail closed; legacy is never a fallback for errors
    if runtime_build_sha is not None:
        if not _FULL_SHA.match(runtime_build_sha):
            return None
        if running_digest is not None and _DIGEST_REF.match(running_digest):
            return RollbackTarget(runtime_build_sha, running_digest, legacy=False)
        return None  # attested build but no immutable digest -> unavailable (no legacy fallback)
    if legacy is not None:
        return RollbackTarget(legacy.source_sha_evidence, legacy.image_digest, legacy=True)
    return None


def legacy_rollback_verified(
    *, running_digest: str, expected_digest: str, health_ok: bool, startup_ok: bool, ready_ok: bool
) -> bool:
    """One-time legacy rollback verification: exact digest + health, never SHA.

    A healthy container alone is not sufficient — the exact preserved immutable
    digest must be running, and all health probes must pass. This is reported as
    a distinct legacy verification, never as a SHA-verified rollback.
    """
    return (
        running_digest == expected_digest
        and _DIGEST_REF.match(running_digest) is not None
        and health_ok
        and startup_ok
        and ready_ok
    )
