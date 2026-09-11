"""Offline production-transport orchestration tests (DEPLOY-2).

Drives ``deploy.transport.deploy`` with a recording FakeExecutor and injected
verify/read-build-sha, proving: strict pre-mutation ordering, fail-closed gates,
backend-only non-destructive mutation, immutable rollback, Dhan-unsafe rollback
handling, single rollback (no loop), and injection-resistant validation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from deploy.executor import ExecResult
from deploy.health_check import VerifyOutcome, VerifyResult
from deploy.transport import DeployConfig, DockerPrivilege, TransportOutcome, deploy

_PREV_SHA = "1" * 40
_TARGET_SHA = "2" * 40
_PREV_DIGEST = "ghcr.io/o/apexscan-backend@sha256:" + "a" * 64
_TARGET_IMAGE = "ghcr.io/o/apexscan-backend@sha256:" + "b" * 64
_DEPLOY_ROOT = "/opt/apexscan/releases"
_RELEASE = f"{_DEPLOY_ROOT}/{_TARGET_SHA}"


class FakeExecutor:
    """Records commands and returns programmed results (default: success)."""

    def __init__(self, handler: Callable[[list[str]], ExecResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._handler = handler or (lambda _cmd: ExecResult(0, "", ""))

    def run(self, command: Sequence[str], *, timeout: float | None = None) -> ExecResult:
        cmd = list(command)
        self.calls.append(cmd)
        return self._handler(cmd)


def _has(cmd: list[str], *tokens: str) -> bool:
    return all(token in cmd for token in tokens)


def _healthy_handler(cmd: list[str]) -> ExecResult:
    if _has(cmd, "docker", "compose", "version", "--short"):
        return ExecResult(0, "2.29.1", "")
    if _has(cmd, "ps", "--format", "backend"):
        return ExecResult(0, _PREV_DIGEST, "")
    return ExecResult(0, "", "")


def _cfg(**over: object) -> DeployConfig:
    base = dict(
        target_image=_TARGET_IMAGE,
        target_sha=_TARGET_SHA,
        deploy_root=_DEPLOY_ROOT,
        base_url="https://apex.example",
        dhan_restart_safe=True,
    )
    base.update(over)
    return DeployConfig(**base)  # type: ignore[arg-type]


def _verify_const(outcome: VerifyOutcome) -> Callable[[str], VerifyResult]:
    return lambda _sha: VerifyResult(outcome, 1, None)


def _verify_by_sha(mapping: dict[str, VerifyOutcome]) -> Callable[[str], VerifyResult]:
    return lambda sha: VerifyResult(mapping[sha], 1, None)


def _run(
    executor: FakeExecutor,
    cfg: DeployConfig,
    verify,
    *,
    responded: bool = True,
    prev_sha: str | None = _PREV_SHA,
    verify_legacy: Callable[[str], bool] = lambda _d: True,
):
    return deploy(
        executor,
        cfg,
        verify=verify,
        verify_legacy=verify_legacy,
        probe_version=lambda: (responded, prev_sha),
        now=lambda: 0.0,
    )


def _flat(executor: FakeExecutor) -> str:
    return " | ".join(" ".join(c) for c in executor.calls)


# --------------------------------------------------------------------------- #
# happy path + ordering + non-destructive
# --------------------------------------------------------------------------- #
def test_success_backend_only_and_ordered() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.SUCCESS
    assert audit.previous_sha == _PREV_SHA and audit.previous_digest == _PREV_DIGEST
    flat = _flat(ex)
    # ordering: config (preflight) < ps (capture) < pull < up (mutation)
    assert flat.index("config") < flat.index("ps ") < flat.index("pull") < flat.index("up -d")
    # backend-only, no-build, and no dependency recreation (backend depends_on pg/redis)
    up = next(c for c in ex.calls if "up" in c)
    assert up[-1] == "backend" and "--no-build" in up and "--no-deps" in up


def test_no_destructive_or_migration_commands() -> None:
    ex = FakeExecutor(_healthy_handler)
    _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    tokens = {token for call in ex.calls for token in call}
    for forbidden in ("down", "-v", "prune", "rm", "volume", "alembic", "--build"):
        assert forbidden not in tokens, f"destructive/build token leaked: {forbidden}"


# --------------------------------------------------------------------------- #
# local validation — fail closed, no remote calls
# --------------------------------------------------------------------------- #
def test_migrations_required_fails_closed_without_contact() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(ex, _cfg(require_migrations=True), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.MIGRATIONS_REQUIRED_NOT_AUTHORIZED
    assert ex.calls == []


def test_mutable_target_image_rejected_without_contact() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(
        ex,
        _cfg(target_image="ghcr.io/o/apexscan-backend:latest"),
        _verify_const(VerifyOutcome.SUCCESS),
    )
    assert audit.outcome is TransportOutcome.TARGET_IMAGE_INVALID
    assert ex.calls == []


def test_unrelated_repository_digest_rejected() -> None:
    ex = FakeExecutor(_healthy_handler)
    evil = "evil.io/attacker/malware@sha256:" + "c" * 64
    audit = _run(ex, _cfg(target_image=evil), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.TARGET_IMAGE_INVALID
    assert ex.calls == []


def test_path_traversal_in_deploy_root_rejected_without_contact() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(ex, _cfg(deploy_root="/opt/../etc"), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.RELEASE_PATH_INVALID
    assert ex.calls == []


# --------------------------------------------------------------------------- #
# remote preflight failures — no mutation
# --------------------------------------------------------------------------- #
def _fail_on(
    match: Callable[[list[str]], bool], result: ExecResult
) -> Callable[[list[str]], ExecResult]:
    def handler(cmd: list[str]) -> ExecResult:
        if match(cmd):
            return result
        return _healthy_handler(cmd)

    return handler


def test_ssh_host_verification_failure() -> None:
    ex = FakeExecutor(
        _fail_on(lambda c: c == ["true"], ExecResult(255, "", "Host key verification failed."))
    )
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.SSH_HOST_VERIFICATION_FAILED
    assert "up -d" not in _flat(ex)


def test_ssh_connection_failure() -> None:
    ex = FakeExecutor(
        _fail_on(lambda c: c == ["true"], ExecResult(255, "", "Connection timed out"))
    )
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.SSH_CONNECTION_FAILED


def test_docker_unavailable() -> None:
    ex = FakeExecutor(_fail_on(lambda c: _has(c, "docker", "info"), ExecResult(1, "", "no daemon")))
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.DOCKER_UNAVAILABLE
    assert "pull" not in _flat(ex)


def test_compose_version_unsupported() -> None:
    ex = FakeExecutor(
        _fail_on(lambda c: _has(c, "compose", "version", "--short"), ExecResult(0, "2.20.0", ""))
    )
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.COMPOSE_VERSION_UNSUPPORTED


def test_compose_config_invalid() -> None:
    ex = FakeExecutor(_fail_on(lambda c: _has(c, "config", "-q"), ExecResult(1, "", "bad overlay")))
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.COMPOSE_CONFIG_INVALID
    assert "up -d" not in _flat(ex)


# --------------------------------------------------------------------------- #
# rollback target + pull
# --------------------------------------------------------------------------- #
def test_legacy_artifact_required_when_no_build_sha_and_no_artifact() -> None:
    # /version responds without build_sha (legacy) but no legacy artifact provided.
    ex = FakeExecutor(_healthy_handler)
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS), prev_sha=None)
    assert audit.outcome is TransportOutcome.LEGACY_ROLLBACK_ARTIFACT_REQUIRED
    assert "up -d" not in _flat(ex)


def test_rollback_target_unavailable_when_running_image_is_mutable() -> None:
    def handler(cmd: list[str]) -> ExecResult:
        if _has(cmd, "ps", "--format", "backend"):
            return ExecResult(0, "ghcr.io/o/apexscan-backend:latest", "")
        return _healthy_handler(cmd)

    ex = FakeExecutor(handler)
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE
    assert "up -d" not in _flat(ex)


def test_target_pull_failure_leaves_backend_untouched() -> None:
    ex = FakeExecutor(
        _fail_on(lambda c: _has(c, "docker", "pull"), ExecResult(1, "", "pull failed"))
    )
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.TARGET_IMAGE_PULL_FAILED
    assert "up -d" not in _flat(ex)


# --------------------------------------------------------------------------- #
# verification failure -> immutable rollback
# --------------------------------------------------------------------------- #
def test_wrong_sha_triggers_immutable_rollback() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.WRONG_SHA, _PREV_SHA: VerifyOutcome.SUCCESS}
    )
    audit = _run(ex, _cfg(), verify)
    assert audit.outcome is TransportOutcome.ROLLED_BACK
    assert audit.verification == "wrong_build_sha"
    assert audit.rollback_attempted and audit.rollback_result == "success"
    # rollback pulled and deployed the PREVIOUS digest
    assert any(_has(c, "docker", "pull", _PREV_DIGEST) for c in ex.calls)


def test_health_failure_triggers_rollback() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.SUCCESS}
    )
    audit = _run(ex, _cfg(), verify)
    assert audit.outcome is TransportOutcome.ROLLED_BACK
    assert audit.verification == "health_failed"


def test_rollback_verification_failure() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.HEALTH_FAILED}
    )
    audit = _run(ex, _cfg(), verify)
    assert audit.outcome is TransportOutcome.ROLLBACK_FAILED
    assert audit.rollback_attempted


def test_deployment_command_failure_triggers_rollback() -> None:
    ex = FakeExecutor(
        _fail_on(lambda c: _has(c, "up", "-d", "--no-build"), ExecResult(1, "", "boom"))
    )
    # up fails for target; rollback up also matches the fail rule -> rollback fails
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.verification == "deployment_command_failed"
    assert audit.rollback_attempted


def test_dhan_unsafe_rollback_requires_operator_no_restart() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.SUCCESS}
    )
    audit = _run(ex, _cfg(dhan_restart_safe=False), verify)
    assert audit.outcome is TransportOutcome.ROLLBACK_REQUIRES_OPERATOR_INTERVENTION
    assert not audit.rollback_attempted
    # no rollback restart happened: only the ONE target 'up' was issued
    assert sum(1 for c in ex.calls if _has(c, "up", "-d")) == 1


# --------------------------------------------------------------------------- #
# B2 — sudo privilege + pinned project name
# --------------------------------------------------------------------------- #
def test_pins_project_name_to_avoid_volume_drift() -> None:
    ex = FakeExecutor(_healthy_handler)
    _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    up = next(c for c in ex.calls if "up" in c)
    assert "-p" in up and up[up.index("-p") + 1] == "apexscan"


def test_direct_mode_has_no_sudo_prefix() -> None:
    ex = FakeExecutor(_healthy_handler)
    _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS))
    assert all("sudo" not in c for c in ex.calls)


def test_sudo_mode_prefixes_docker_and_fs_with_sudo_dash_n() -> None:
    ex = FakeExecutor(_healthy_handler)
    _run(
        ex,
        _cfg(docker_privilege=DockerPrivilege.SUDO_NON_INTERACTIVE),
        _verify_const(VerifyOutcome.SUCCESS),
    )
    docker_calls = [c for c in ex.calls if "docker" in c or ("test" in c and "-d" in c)]
    for c in docker_calls:
        assert c[0] == "sudo" and c[1] == "-n"  # exactly `sudo -n`, no arbitrary prefix
    assert not any("-S" in c for c in ex.calls)  # never sudo -S (no password piping)


def test_sudo_password_required_fails_closed_before_mutation() -> None:
    ex = FakeExecutor(
        _fail_on(lambda c: c == ["sudo", "-n", "true"], ExecResult(1, "", "a password is required"))
    )
    audit = _run(
        ex,
        _cfg(docker_privilege=DockerPrivilege.SUDO_NON_INTERACTIVE),
        _verify_const(VerifyOutcome.SUCCESS),
    )
    assert audit.outcome is TransportOutcome.DOCKER_PRIVILEGE_UNAVAILABLE
    assert "up -d" not in _flat(ex)


def test_single_rollback_no_loop() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.HEALTH_FAILED}
    )
    _run(ex, _cfg(), verify)
    # exactly two 'up' mutations total: target + one rollback attempt (no loop)
    assert sum(1 for c in ex.calls if _has(c, "up", "-d")) == 2


# --------------------------------------------------------------------------- #
# B (DEPLOY-3C-R2) — versioned release path
# --------------------------------------------------------------------------- #
def test_release_path_resolves_under_root() -> None:
    from deploy.transport import resolve_release_path

    assert resolve_release_path(_DEPLOY_ROOT, _TARGET_SHA) == _RELEASE


def test_release_path_rejects_short_and_nonhex_and_traversal() -> None:
    import pytest

    from deploy.transport import ReleasePathError, resolve_release_path

    for root, sha in [
        (_DEPLOY_ROOT, "2" * 12),  # short SHA
        (_DEPLOY_ROOT, "g" * 40),  # non-hex
        (_DEPLOY_ROOT, "2" * 39),  # wrong length
        ("relative/root", _TARGET_SHA),  # non-absolute root
        ("/opt/../etc", _TARGET_SHA),  # traversal in root
    ]:
        with pytest.raises(ReleasePathError):
            resolve_release_path(root, sha)


def test_target_and_rollback_use_same_versioned_release_authority() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.SUCCESS}
    )
    _run(ex, _cfg(), verify)
    ups = [c for c in ex.calls if _has(c, "up", "-d")]
    assert len(ups) == 2  # target + rollback
    for c in ups:
        joined = " ".join(c)
        assert f"{_RELEASE}/docker-compose.production.yml" in joined
        assert "-p apexscan" in joined and "--no-deps" in c and "--no-build" in c


# --------------------------------------------------------------------------- #
# B10-19 (DEPLOY-3C-R2) — first-deploy legacy rollback wiring
# --------------------------------------------------------------------------- #
_LEGACY_DIGEST = "ghcr.io/o/apexscan-backend@sha256:" + "c" * 64


def _legacy():
    from deploy.legacy import LegacyRollbackArtifact, SourceShaEvidenceKind

    return LegacyRollbackArtifact(
        image_digest=_LEGACY_DIGEST,
        running_image_id="sha256:" + "d" * 64,
        source_sha_evidence="7" * 40,
        source_sha_evidence_kind=SourceShaEvidenceKind.IMAGE_TAG,
        provenance="preserved",
    )


def test_broken_version_fails_closed_no_mutation() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(
        ex, _cfg(legacy_artifact=_legacy()), _verify_const(VerifyOutcome.SUCCESS), responded=False
    )
    assert audit.outcome is TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE
    assert "up -d" not in _flat(ex)  # never enters legacy on a broken /version


def test_legacy_rollback_on_target_failure() -> None:
    ex = FakeExecutor(_healthy_handler)
    # legacy backend (no build_sha) + valid artifact; target deploy fails health
    audit = _run(
        ex,
        _cfg(legacy_artifact=_legacy()),
        _verify_const(VerifyOutcome.HEALTH_FAILED),
        prev_sha=None,
    )
    assert audit.outcome is TransportOutcome.LEGACY_ROLLED_BACK
    assert audit.previous_digest == _LEGACY_DIGEST and audit.rollback_attempted
    assert any(_has(c, "docker", "pull", _LEGACY_DIGEST) for c in ex.calls)


def test_legacy_rollback_verification_failure() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(
        ex,
        _cfg(legacy_artifact=_legacy()),
        _verify_const(VerifyOutcome.HEALTH_FAILED),
        prev_sha=None,
        verify_legacy=lambda _d: False,
    )
    assert audit.outcome is TransportOutcome.ROLLBACK_FAILED
    assert audit.rollback_result == "legacy_verification_failed"


def test_modern_deploy_ignores_legacy_artifact() -> None:
    ex = FakeExecutor(_healthy_handler)
    # build_sha present -> normal rollback; legacy verify would fail if wrongly used.
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.SUCCESS}
    )
    audit = _run(ex, _cfg(legacy_artifact=_legacy()), verify, verify_legacy=lambda _d: False)
    assert audit.outcome is TransportOutcome.ROLLED_BACK  # normal path used, not legacy
    assert audit.previous_digest == _PREV_DIGEST  # running digest, not the legacy digest


def test_malformed_build_sha_fails_closed_not_legacy() -> None:
    # A non-hex/"unknown" build_sha is neither a valid modern SHA nor legacy: it
    # must fail closed (never downgrade to legacy), even with an artifact present.
    ex = FakeExecutor(_healthy_handler)
    audit = _run(
        ex,
        _cfg(legacy_artifact=_legacy()),
        _verify_const(VerifyOutcome.SUCCESS),
        prev_sha="unknown",
    )
    assert audit.outcome is TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE
    assert "up -d" not in _flat(ex)


def test_legacy_args_construction_contract() -> None:
    import argparse

    import pytest

    from deploy.legacy import LegacyArtifactError
    from deploy.transport import _legacy_artifact_from_args

    def _args(**kw) -> argparse.Namespace:
        base = dict(
            legacy_digest="",
            legacy_image_id="sha256:" + "d" * 64,
            legacy_source_sha="7" * 40,
            legacy_evidence_kind="image_tag",
            legacy_provenance="p",
        )
        base.update(kw)
        return argparse.Namespace(**base)

    assert _legacy_artifact_from_args(_args()) is None  # no digest -> no artifact
    with pytest.raises(LegacyArtifactError):  # malformed digest -> raises (main catches -> None)
        _legacy_artifact_from_args(_args(legacy_digest="evil/malware@sha256:" + "a" * 64))


# --------------------------------------------------------------------------- #
# _running_digest — modern rollback digest capture (DEPLOY-TRANSPORT-FIX-1)
# --------------------------------------------------------------------------- #
def test_running_digest_supplies_apexscan_image_and_returns_running_image() -> None:
    # Production Compose needs APEXSCAN_IMAGE to interpolate even for `ps`; the fix
    # supplies the (deploy) target image only to parse — but `ps` reports the RUNNING
    # container's image, so the captured rollback digest is the current image, not target.
    from deploy.transport import _running_digest

    def handler(cmd: list[str]) -> ExecResult:
        if _has(cmd, "ps", "--format", "backend"):
            return ExecResult(0, _PREV_DIGEST, "")  # running image A
        return ExecResult(0, "", "")

    ex = FakeExecutor(handler)
    digest = _running_digest(ex, _cfg())
    assert digest == _PREV_DIGEST  # running image A ...
    assert digest != _TARGET_IMAGE  # ... never the deploy target B
    ps_call = next(c for c in ex.calls if _has(c, "ps", "--format", "backend"))
    assert f"APEXSCAN_IMAGE={_TARGET_IMAGE}" in ps_call  # interpolation satisfied
    assert "env" in ps_call
    # read-only: no mutating compose subcommands
    for verb in ("up", "create", "pull", "restart", "stop", "rm", "down"):
        assert verb not in ps_call


def test_running_digest_ps_failure_fails_closed() -> None:
    from deploy.transport import _running_digest

    ex = FakeExecutor(
        lambda cmd: ExecResult(1, "", "boom") if _has(cmd, "ps") else ExecResult(0, "", "")
    )
    assert _running_digest(ex, _cfg()) is None


def test_running_digest_empty_output_fails_closed() -> None:
    from deploy.transport import _running_digest

    ex = FakeExecutor(
        lambda cmd: ExecResult(0, "  \n", "") if _has(cmd, "ps") else ExecResult(0, "", "")
    )
    assert _running_digest(ex, _cfg()) is None


def test_running_digest_malformed_output_fails_closed() -> None:
    from deploy.transport import _running_digest

    for bad in ("not-a-digest", "postgres:17-alpine", "ghcr.io/o/other@sha256:" + "a" * 64):
        ex = FakeExecutor(
            lambda cmd, b=bad: ExecResult(0, b, "") if _has(cmd, "ps") else ExecResult(0, "", "")
        )
        assert _running_digest(ex, _cfg()) is None
