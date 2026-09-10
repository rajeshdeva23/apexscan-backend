"""Legacy rollback bridge: model, selection invariant, verification (DEPLOY-3A/B1)."""

from __future__ import annotations

import pytest

from deploy.legacy import (
    LegacyArtifactError,
    LegacyRollbackArtifact,
    SourceShaEvidenceKind,
    legacy_rollback_verified,
    select_rollback_target,
)

_SHA = "7" * 40
_NEW_SHA = "6" * 40
_DIGEST = "ghcr.io/o/apexscan-backend@sha256:" + "a" * 64
_NEW_DIGEST = "ghcr.io/o/apexscan-backend@sha256:" + "b" * 64
_IMAGE_ID = "sha256:" + "c" * 64


def _artifact(**over: object) -> LegacyRollbackArtifact:
    base = dict(
        image_digest=_DIGEST,
        running_image_id=_IMAGE_ID,
        source_sha_evidence=_SHA,
        source_sha_evidence_kind=SourceShaEvidenceKind.IMAGE_TAG,
        provenance="preserved running image",
    )
    base.update(over)
    return LegacyRollbackArtifact(**base)  # type: ignore[arg-type]


def test_valid_artifact() -> None:
    art = _artifact()
    assert art.legacy_unverified_build_sha is True
    assert art.image_digest == _DIGEST


@pytest.mark.parametrize(
    "over",
    [
        {"image_digest": "apexscan-backend:latest"},
        {"image_digest": "evil/malware@sha256:" + "a" * 64},
        {"running_image_id": "213ddff"},
        {"source_sha_evidence": "7abc"},
        {"legacy_unverified_build_sha": False},
    ],
)
def test_malformed_artifact_rejected(over: dict[str, object]) -> None:
    with pytest.raises(LegacyArtifactError):
        _artifact(**over)


def test_normal_path_when_build_sha_and_digest() -> None:
    target = select_rollback_target(
        runtime_build_sha=_NEW_SHA, running_digest=_NEW_DIGEST, legacy=None
    )
    assert target is not None and target.legacy is False
    assert target.source_sha == _NEW_SHA and target.image_digest == _NEW_DIGEST


def test_normal_path_without_digest_is_unavailable() -> None:
    assert (
        select_rollback_target(runtime_build_sha=_NEW_SHA, running_digest=None, legacy=None) is None
    )


def test_legacy_used_only_when_no_build_sha() -> None:
    target = select_rollback_target(runtime_build_sha=None, running_digest=None, legacy=_artifact())
    assert target is not None and target.legacy is True
    assert target.image_digest == _DIGEST


def test_legacy_ignored_after_sha_pinned_deploy() -> None:
    # build_sha present -> normal semantics mandatory; legacy artifact must be ignored.
    target = select_rollback_target(
        runtime_build_sha=_NEW_SHA, running_digest=_NEW_DIGEST, legacy=_artifact()
    )
    assert target is not None and target.legacy is False
    assert target.image_digest == _NEW_DIGEST  # NOT the legacy digest


def test_no_target_without_build_sha_or_legacy() -> None:
    assert select_rollback_target(runtime_build_sha=None, running_digest=None, legacy=None) is None


def test_legacy_verification_requires_exact_digest_and_health() -> None:
    ok = legacy_rollback_verified(
        running_digest=_DIGEST,
        expected_digest=_DIGEST,
        health_ok=True,
        startup_ok=True,
        ready_ok=True,
    )
    assert ok is True


def test_legacy_verification_rejects_wrong_digest_even_if_healthy() -> None:
    assert not legacy_rollback_verified(
        running_digest=_NEW_DIGEST,
        expected_digest=_DIGEST,
        health_ok=True,
        startup_ok=True,
        ready_ok=True,
    )


@pytest.mark.parametrize("probe", ["health_ok", "startup_ok", "ready_ok"])
def test_legacy_verification_requires_all_health(probe: str) -> None:
    kwargs = {"health_ok": True, "startup_ok": True, "ready_ok": True, probe: False}
    assert not legacy_rollback_verified(running_digest=_DIGEST, expected_digest=_DIGEST, **kwargs)
