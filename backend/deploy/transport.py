"""Production deployment transport orchestration (DEPLOY-2).

Drives a promotion over a :class:`~deploy.executor.RemoteExecutor`: remote
preflight, immutable rollback-target capture, digest pull, backend-only update,
bounded health/version verification, and immutable rollback on failure. Every
mandatory gate runs before any mutation and fails closed; nothing here starts a
retry/rollback loop or authenticates Dhan. Pure orchestration — the executor and
the verifier are injected, so the whole flow is exercised offline with fakes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from deploy.executor import ExecResult, RemoteExecutor
from deploy.health_check import VerifyOutcome, VerifyResult
from deploy.rollback import RollbackDecision, is_immutable_ref, plan_rollback

# Digest-pinned reference bound to the ApexScan backend image name (the repo
# component must be ``apexscan-backend``): a syntactically valid digest from an
# unrelated image must not be promotable.
_DIGEST_REF = re.compile(r"^([a-z0-9][a-z0-9./_-]*/)?apexscan-backend@sha256:[0-9a-f]{64}$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_MIN_COMPOSE = (2, 24)
_BACKEND = "backend"


class TransportOutcome(StrEnum):
    """Terminal outcome of a deployment attempt (no generic pass/fail)."""

    SUCCESS = "success"
    TRANSPORT_NOT_CONFIGURED = "transport_not_configured"
    MIGRATIONS_REQUIRED_NOT_AUTHORIZED = "migrations_required_not_authorized"
    TARGET_IMAGE_INVALID = "target_image_invalid"
    REMOTE_PATH_INVALID = "remote_path_invalid"
    SSH_HOST_VERIFICATION_FAILED = "ssh_host_verification_failed"
    SSH_CONNECTION_FAILED = "ssh_connection_failed"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    DOCKER_PRIVILEGE_UNAVAILABLE = "docker_privilege_unavailable"
    COMPOSE_UNAVAILABLE = "compose_unavailable"
    COMPOSE_VERSION_UNSUPPORTED = "compose_version_unsupported"
    COMPOSE_CONFIG_INVALID = "compose_config_invalid"
    ROLLBACK_TARGET_UNAVAILABLE = "rollback_target_unavailable"
    TARGET_IMAGE_PULL_FAILED = "target_image_pull_failed"
    DEPLOYMENT_COMMAND_FAILED = "deployment_command_failed"
    HEALTH_FAILED = "health_failed"
    STARTUP_FAILED = "startup_failed"
    READINESS_FAILED = "readiness_failed"
    WRONG_BUILD_SHA = "wrong_build_sha"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    ROLLBACK_REQUIRES_OPERATOR_INTERVENTION = "rollback_requires_operator_intervention"


_VERIFY_TO_OUTCOME = {
    VerifyOutcome.HEALTH_FAILED: TransportOutcome.HEALTH_FAILED,
    VerifyOutcome.STARTUP_FAILED: TransportOutcome.STARTUP_FAILED,
    VerifyOutcome.READINESS_FAILED: TransportOutcome.READINESS_FAILED,
    VerifyOutcome.WRONG_SHA: TransportOutcome.WRONG_BUILD_SHA,
    VerifyOutcome.TIMEOUT: TransportOutcome.HEALTH_FAILED,
}


@dataclass(frozen=True, slots=True)
class DeploymentAudit:
    """Bounded, secret-free record of a deployment attempt."""

    run_id: str
    requested_sha: str
    target_digest: str
    previous_sha: str | None
    previous_digest: str | None
    started_at: float
    completed_at: float
    verification: str
    rollback_attempted: bool
    rollback_result: str | None
    outcome: TransportOutcome


class DockerPrivilege(StrEnum):
    """How Docker is invoked on the host (closed set — never arbitrary shell)."""

    DIRECT = "direct"  # `docker ...` (user is in the docker group / local tests)
    SUDO_NON_INTERACTIVE = "sudo_non_interactive"  # `sudo -n docker ...` (prod ubuntu)


# Fixed, closed command prefixes — no caller-supplied prefix strings are allowed.
_PRIVILEGE_PREFIX: dict[DockerPrivilege, tuple[str, ...]] = {
    DockerPrivilege.DIRECT: (),
    DockerPrivilege.SUDO_NON_INTERACTIVE: ("sudo", "-n"),
}


@dataclass(frozen=True, slots=True)
class DeployConfig:
    """Inputs describing one promotion (no secrets; SSH lives in the executor)."""

    target_image: str
    target_sha: str
    deploy_path: str
    base_url: str
    dhan_restart_safe: bool
    require_migrations: bool = False
    run_id: str = "local"
    docker_privilege: DockerPrivilege = DockerPrivilege.DIRECT
    # Existing production Compose project (derived from the running container's
    # com.docker.compose.project label). Pinned so the deploy attaches to the
    # SAME project/volumes/networks instead of the deploy-dir-derived default.
    project_name: str = "apexscan"
    # The single production-authority Compose file (DEPLOY-3B). Self-contained;
    # not the developer base + overlay. Resolved inside ``deploy_path``.
    compose_file: str = "docker-compose.production.yml"


def _priv(cfg: DeployConfig, *args: str) -> list[str]:
    """Prefix a host command with the configured (closed) privilege escalation."""
    return [*_PRIVILEGE_PREFIX[cfg.docker_privilege], *args]


def _compose(cfg: DeployConfig, *, image: str | None = None) -> list[str]:
    """Compose invocation: privilege prefix, then optional in-command APEXSCAN_IMAGE.

    The ``env APEXSCAN_IMAGE=…`` assignment is placed AFTER the privilege prefix so
    it survives ``sudo`` (which resets the environment); ``sudo -n env VAR=… docker
    compose`` sets the variable for the root-run compose, whereas ``env VAR=… sudo``
    would be stripped. The project name is pinned so the deploy attaches to the
    existing project's volumes/networks, never a deploy-dir-derived new project.
    """
    env_assignment = ["env", f"APEXSCAN_IMAGE={image}"] if image is not None else []
    return _priv(
        cfg,
        *env_assignment,
        "docker",
        "compose",
        "-p",
        cfg.project_name,
        "-f",
        f"{cfg.deploy_path}/{cfg.compose_file}",
        "--project-directory",
        cfg.deploy_path,
    )


def _classify_ssh_failure(result: ExecResult) -> TransportOutcome:
    """Distinguish a host-key verification failure from a plain connection failure."""
    text = result.stderr.lower()
    if "host key" in text or "known_hosts" in text or "verification failed" in text:
        return TransportOutcome.SSH_HOST_VERIFICATION_FAILED
    return TransportOutcome.SSH_CONNECTION_FAILED


def _compose_version_ok(raw: str) -> bool:
    """Whether ``docker compose version --short`` meets the overlay's minimum."""
    match = re.match(r"v?(\d+)\.(\d+)", raw.strip())
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= _MIN_COMPOSE


def _preflight(executor: RemoteExecutor, cfg: DeployConfig) -> TransportOutcome | None:
    """Run all remote pre-mutation checks in order; return the first failure or None."""
    reachable = executor.run(["true"])
    if not reachable.ok:
        return _classify_ssh_failure(reachable)
    if (
        cfg.docker_privilege is DockerPrivilege.SUDO_NON_INTERACTIVE
        and not executor.run(["sudo", "-n", "true"]).ok
    ):
        return TransportOutcome.DOCKER_PRIVILEGE_UNAVAILABLE
    if not executor.run(_priv(cfg, "test", "-d", cfg.deploy_path)).ok:
        return TransportOutcome.REMOTE_PATH_INVALID
    if not executor.run(_priv(cfg, "docker", "info")).ok:
        return TransportOutcome.DOCKER_UNAVAILABLE
    version = executor.run(_priv(cfg, "docker", "compose", "version", "--short"))
    if not version.ok:
        return TransportOutcome.COMPOSE_UNAVAILABLE
    if not _compose_version_ok(version.stdout):
        return TransportOutcome.COMPOSE_VERSION_UNSUPPORTED
    config = executor.run([*_compose(cfg, image=cfg.target_image), "config", "-q"])
    if not config.ok:
        return TransportOutcome.COMPOSE_CONFIG_INVALID
    return None


def _capture_previous(
    executor: RemoteExecutor, cfg: DeployConfig, read_build_sha: Callable[[], str | None]
) -> tuple[str, str] | None:
    """Capture (previous_sha, previous_digest) for an immutable rollback, or None."""
    previous_sha = read_build_sha()
    if previous_sha is None or not _FULL_SHA.match(previous_sha):
        return None
    result = executor.run([*_compose(cfg), "ps", "--format", "{{.Image}}", _BACKEND])
    digest = result.stdout.strip().splitlines()[0] if result.ok and result.stdout.strip() else ""
    if not is_immutable_ref(digest) or not _DIGEST_REF.match(digest):
        return None
    return previous_sha, digest


def _pull(executor: RemoteExecutor, cfg: DeployConfig, image: str) -> bool:
    """Pull the immutable target image before any mutation."""
    return executor.run(_priv(cfg, "docker", "pull", image)).ok


def _update_backend(executor: RemoteExecutor, cfg: DeployConfig, image: str) -> bool:
    """Backend-only, no-build Compose update to ``image``.

    ``--no-deps`` is required: the backend ``depends_on`` postgres and redis, so a
    plain ``up backend`` would (re)create those data services. ``--no-deps`` keeps
    the mutation strictly to the backend and never touches Postgres/Redis/volumes.
    """
    return executor.run(
        [*_compose(cfg, image=image), "up", "-d", "--no-deps", "--no-build", _BACKEND]
    ).ok


def _validate(cfg: DeployConfig) -> TransportOutcome | None:
    """Local, pre-network validation of the deployment request (fail closed)."""
    if cfg.require_migrations:
        return TransportOutcome.MIGRATIONS_REQUIRED_NOT_AUTHORIZED
    if not _DIGEST_REF.match(cfg.target_image):
        return TransportOutcome.TARGET_IMAGE_INVALID
    if not _FULL_SHA.match(cfg.target_sha):
        return TransportOutcome.TARGET_IMAGE_INVALID
    if ".." in cfg.deploy_path or not _REMOTE_PATH.match(cfg.deploy_path):
        return TransportOutcome.REMOTE_PATH_INVALID
    return None


def _rollback(
    executor: RemoteExecutor,
    cfg: DeployConfig,
    previous: tuple[str, str],
    verify: Callable[[str], VerifyResult],
    build_audit: Callable[..., DeploymentAudit],
) -> DeploymentAudit:
    """Attempt a single immutable rollback to the previous digest (no loop)."""
    previous_sha, previous_digest = previous
    plan = plan_rollback(
        previous_artifact=previous_digest,
        restart_required=True,
        dhan_restart_safety_confirmed=cfg.dhan_restart_safe,
    )
    if plan.decision is RollbackDecision.REQUIRES_OPERATOR_INTERVENTION:
        return build_audit(
            TransportOutcome.ROLLBACK_REQUIRES_OPERATOR_INTERVENTION, False, "dhan_unsafe"
        )
    if plan.decision is RollbackDecision.UNAVAILABLE:
        return build_audit(TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE, False, None)
    if not _pull(executor, cfg, previous_digest):
        return build_audit(TransportOutcome.ROLLBACK_FAILED, True, "pull_failed")
    if not _update_backend(executor, cfg, previous_digest):
        return build_audit(TransportOutcome.ROLLBACK_FAILED, True, "update_failed")
    verified = verify(previous_sha)
    if verified.outcome is VerifyOutcome.SUCCESS:
        return build_audit(TransportOutcome.ROLLED_BACK, True, "success")
    return build_audit(TransportOutcome.ROLLBACK_FAILED, True, verified.outcome.value)


def _pre_mutation_gate(
    executor: RemoteExecutor, cfg: DeployConfig, read_build_sha: Callable[[], str | None]
) -> tuple[TransportOutcome | None, tuple[str, str] | None]:
    """Run every pre-mutation gate in order; return (failure_or_None, previous_or_None).

    Ordering: validate -> preflight -> capture rollback target -> pull. On the pull
    failure the captured ``previous`` is still returned so the audit records it; no
    mutation has occurred in any failing path.
    """
    invalid = _validate(cfg)
    if invalid is not None:
        return invalid, None
    preflight_failure = _preflight(executor, cfg)
    if preflight_failure is not None:
        return preflight_failure, None
    previous = _capture_previous(executor, cfg, read_build_sha)
    if previous is None:
        return TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE, None
    if not _pull(executor, cfg, cfg.target_image):
        return TransportOutcome.TARGET_IMAGE_PULL_FAILED, previous
    return None, previous


def deploy(
    executor: RemoteExecutor,
    cfg: DeployConfig,
    *,
    verify: Callable[[str], VerifyResult],
    read_build_sha: Callable[[], str | None],
    now: Callable[[], float] = time.time,
) -> DeploymentAudit:
    """Promote ``cfg.target_image`` to production, rolling back on any failure.

    Enforces the ordering: validate -> preflight -> capture rollback target ->
    pull -> mutate -> verify, and on post-mutation failure -> rollback -> verify.
    No mutation occurs until every pre-mutation gate passes; fails closed.
    """
    started = now()
    failure, previous = _pre_mutation_gate(executor, cfg, read_build_sha)

    def audit(
        outcome: TransportOutcome,
        *,
        verification: str = "not_started",
        rollback_attempted: bool = False,
        rollback_result: str | None = None,
    ) -> DeploymentAudit:
        prev_sha, prev_digest = previous or (None, None)
        return DeploymentAudit(
            run_id=cfg.run_id,
            requested_sha=cfg.target_sha,
            target_digest=cfg.target_image,
            previous_sha=prev_sha,
            previous_digest=prev_digest,
            started_at=started,
            completed_at=now(),
            verification=verification,
            rollback_attempted=rollback_attempted,
            rollback_result=rollback_result,
            outcome=outcome,
        )

    if failure is not None:
        return audit(failure)
    assert previous is not None  # gate guarantees it on the success path

    def rollback_audit(
        outcome: TransportOutcome, attempted: bool, result: str | None, *, failure: str
    ) -> DeploymentAudit:
        return audit(
            outcome, verification=failure, rollback_attempted=attempted, rollback_result=result
        )

    if not _update_backend(executor, cfg, cfg.target_image):
        return _rollback(
            executor,
            cfg,
            previous,
            verify,
            lambda o, a, r: rollback_audit(o, a, r, failure="deployment_command_failed"),
        )
    result = verify(cfg.target_sha)
    if result.outcome is VerifyOutcome.SUCCESS:
        return audit(TransportOutcome.SUCCESS, verification="success")
    failure_reason = _VERIFY_TO_OUTCOME[result.outcome].value
    return _rollback(
        executor,
        cfg,
        previous,
        verify,
        lambda o, a, r: rollback_audit(o, a, r, failure=failure_reason),
    )


def _build_verifier(
    base_url: str, *, attempts: int, interval: float, timeout: float
) -> tuple[Callable[[str], VerifyResult], Callable[[], str | None]]:
    """Wire the HTTP probe into a (verify, read_build_sha) pair for real deploys."""
    from deploy.health_check import ProbeResult, probe_endpoints, verify_release

    def probe() -> ProbeResult:
        return probe_endpoints(base_url.rstrip("/"), timeout)

    def verify(expected_sha: str) -> VerifyResult:
        return verify_release(
            probe,
            expected_sha=expected_sha,
            max_attempts=attempts,
            sleep=lambda: time.sleep(interval),
        )

    def read_build_sha() -> str | None:
        return probe().version_sha

    return verify, read_build_sha


def _parse_args(argv: Sequence[str] | None) -> object:
    """Parse the transport CLI arguments."""
    import argparse

    parser = argparse.ArgumentParser(description="Promote an immutable image to production.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--image", required=True, help="Digest-pinned target image reference.")
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--deploy-path", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--dhan-restart-safe", action="store_true")
    parser.add_argument("--require-migrations", action="store_true")
    parser.add_argument(
        "--docker-privilege",
        choices=[p.value for p in DockerPrivilege],
        default=DockerPrivilege.DIRECT.value,
        help="How Docker is invoked on the host (production ubuntu needs sudo_non_interactive).",
    )
    parser.add_argument("--project-name", default="apexscan", help="Existing Compose project name.")
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--run-id", default="local")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: run a production promotion over SSH; exit non-zero unless SUCCESS."""
    from deploy.executor import SSHExecutor

    args = _parse_args(argv)
    executor = SSHExecutor(
        host=args.host,  # type: ignore[attr-defined]
        user=args.user,  # type: ignore[attr-defined]
        key_file=args.key_file,  # type: ignore[attr-defined]
        known_hosts_file=args.known_hosts,  # type: ignore[attr-defined]
    )
    cfg = DeployConfig(
        target_image=args.image,  # type: ignore[attr-defined]
        target_sha=args.target_sha,  # type: ignore[attr-defined]
        deploy_path=args.deploy_path,  # type: ignore[attr-defined]
        base_url=args.base_url,  # type: ignore[attr-defined]
        dhan_restart_safe=args.dhan_restart_safe,  # type: ignore[attr-defined]
        require_migrations=args.require_migrations,  # type: ignore[attr-defined]
        run_id=args.run_id,  # type: ignore[attr-defined]
        docker_privilege=DockerPrivilege(args.docker_privilege),  # type: ignore[attr-defined]
        project_name=args.project_name,  # type: ignore[attr-defined]
    )
    verify, read_build_sha = _build_verifier(
        cfg.base_url,
        attempts=args.attempts,  # type: ignore[attr-defined]
        interval=args.interval,  # type: ignore[attr-defined]
        timeout=args.timeout,  # type: ignore[attr-defined]
    )
    audit = deploy(executor, cfg, verify=verify, read_build_sha=read_build_sha)
    print(
        f"outcome={audit.outcome.value} verification={audit.verification} "
        f"rollback_attempted={audit.rollback_attempted} rollback_result={audit.rollback_result} "
        f"requested_sha={audit.requested_sha} previous_sha={audit.previous_sha}"
    )
    return 0 if audit.outcome is TransportOutcome.SUCCESS else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
