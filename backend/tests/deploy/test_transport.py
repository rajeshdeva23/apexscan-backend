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
from deploy.transport import DeployConfig, TransportOutcome, deploy

_PREV_SHA = "1" * 40
_TARGET_SHA = "2" * 40
_PREV_DIGEST = "ghcr.io/o/apexscan-backend@sha256:" + "a" * 64
_TARGET_IMAGE = "ghcr.io/o/apexscan-backend@sha256:" + "b" * 64
_DEPLOY_PATH = "/opt/apexscan"


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
        deploy_path=_DEPLOY_PATH,
        base_url="https://apex.example",
        dhan_restart_safe=True,
    )
    base.update(over)
    return DeployConfig(**base)  # type: ignore[arg-type]


def _verify_const(outcome: VerifyOutcome) -> Callable[[str], VerifyResult]:
    return lambda _sha: VerifyResult(outcome, 1, None)


def _verify_by_sha(mapping: dict[str, VerifyOutcome]) -> Callable[[str], VerifyResult]:
    return lambda sha: VerifyResult(mapping[sha], 1, None)


def _run(executor: FakeExecutor, cfg: DeployConfig, verify, prev_sha=_PREV_SHA):
    return deploy(executor, cfg, verify=verify, read_build_sha=lambda: prev_sha, now=lambda: 0.0)


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
    # backend-only, no-build
    up = next(c for c in ex.calls if "up" in c)
    assert up[-1] == "backend" and "--no-build" in up


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


def test_path_traversal_rejected_without_contact() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(ex, _cfg(deploy_path="/opt/../etc"), _verify_const(VerifyOutcome.SUCCESS))
    assert audit.outcome is TransportOutcome.REMOTE_PATH_INVALID
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
def test_rollback_target_unavailable_when_no_prev_sha() -> None:
    ex = FakeExecutor(_healthy_handler)
    audit = _run(ex, _cfg(), _verify_const(VerifyOutcome.SUCCESS), prev_sha=None)
    assert audit.outcome is TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE
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


def test_single_rollback_no_loop() -> None:
    ex = FakeExecutor(_healthy_handler)
    verify = _verify_by_sha(
        {_TARGET_SHA: VerifyOutcome.HEALTH_FAILED, _PREV_SHA: VerifyOutcome.HEALTH_FAILED}
    )
    _run(ex, _cfg(), verify)
    # exactly two 'up' mutations total: target + one rollback attempt (no loop)
    assert sum(1 for c in ex.calls if _has(c, "up", "-d")) == 2
