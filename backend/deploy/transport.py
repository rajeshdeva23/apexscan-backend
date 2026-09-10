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
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from deploy.executor import ExecResult, RemoteExecutor
from deploy.health_check import VerifyOutcome, VerifyResult
from deploy.legacy import (
    LegacyArtifactError,
    LegacyRollbackArtifact,
    RollbackTarget,
    SourceShaEvidenceKind,
    legacy_rollback_verified,
    select_rollback_target,
)
from deploy.rollback import RollbackDecision, plan_rollback

# Digest-pinned reference bound to the ApexScan backend image name (the repo
# component must be ``apexscan-backend``): a syntactically valid digest from an
# unrelated image must not be promotable.
_DIGEST_REF = re.compile(r"^([a-z0-9][a-z0-9./_-]*/)?apexscan-backend@sha256:[0-9a-f]{64}$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_MIN_COMPOSE = (2, 24)
_BACKEND = "backend"


class ReleasePathError(ValueError):
    """Raised when a versioned release path cannot be safely resolved."""


def resolve_release_path(deploy_root: str, target_sha: str) -> str:
    """Resolve the versioned release directory ``<deploy_root>/<full-sha>`` (fail closed).

    Path resolution lives in reviewed code, not workflow string concatenation: the
    root must be absolute and traversal-free, the SHA a full 40-char hex commit
    (no short SHA, tag, branch, slash, or ``..``), and the result must remain a
    direct child of the root. Rejects anything that could escape the root.
    """
    if not deploy_root.startswith("/") or ".." in deploy_root.split("/"):
        raise ReleasePathError("deploy_root must be an absolute, traversal-free path")
    if not _REMOTE_PATH.match(deploy_root):
        raise ReleasePathError("deploy_root has invalid characters")
    if not _FULL_SHA.match(target_sha):
        raise ReleasePathError("target_sha must be a full 40-char hex commit")
    root = deploy_root.rstrip("/")
    release = f"{root}/{target_sha}"
    if not release.startswith(f"{root}/"):
        raise ReleasePathError("resolved release path escaped the deploy root")
    return release


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
    LEGACY_ROLLBACK_ARTIFACT_REQUIRED = "legacy_rollback_artifact_required"
    RELEASE_PATH_INVALID = "release_path_invalid"
    TARGET_IMAGE_PULL_FAILED = "target_image_pull_failed"
    DEPLOYMENT_COMMAND_FAILED = "deployment_command_failed"
    HEALTH_FAILED = "health_failed"
    STARTUP_FAILED = "startup_failed"
    READINESS_FAILED = "readiness_failed"
    WRONG_BUILD_SHA = "wrong_build_sha"
    ROLLED_BACK = "rolled_back"
    LEGACY_ROLLED_BACK = "legacy_rolled_back"
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
    deploy_root: str
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
    # not the developer base + overlay. Resolved inside the versioned release dir.
    compose_file: str = "docker-compose.production.yml"
    # First-deploy legacy rollback artifact (DEPLOY-3C-R2). Consulted ONLY when the
    # running backend reports no build_sha; ignored once a SHA-pinned image runs.
    legacy_artifact: LegacyRollbackArtifact | None = None

    @property
    def release_path(self) -> str:
        """The versioned release directory for this exact target SHA."""
        return resolve_release_path(self.deploy_root, self.target_sha)


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
        f"{cfg.release_path}/{cfg.compose_file}",
        "--project-directory",
        cfg.release_path,
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
    if not executor.run(_priv(cfg, "test", "-d", cfg.release_path)).ok:
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


def _running_digest(executor: RemoteExecutor, cfg: DeployConfig) -> str | None:
    """The current backend image reference from Compose, or None if not a digest."""
    result = executor.run([*_compose(cfg), "ps", "--format", "{{.Image}}", _BACKEND])
    digest = result.stdout.strip().splitlines()[0] if result.ok and result.stdout.strip() else ""
    return digest if _DIGEST_REF.match(digest) else None


def _resolve_rollback(
    executor: RemoteExecutor,
    cfg: DeployConfig,
    probe_version: Callable[[], tuple[bool, str | None]],
) -> tuple[TransportOutcome | None, RollbackTarget | None]:
    """Resolve the rollback target before mutation (normal, legacy, or fail closed).

    Reads ``/version`` (responded?, build_sha?) and the running image digest, then
    delegates the selection to :func:`deploy.legacy.select_rollback_target`. A broken
    ``/version`` fails closed; a modern build always uses normal semantics; a
    build_sha-less (legacy) backend uses the explicit legacy artifact or, if absent,
    demands one — never a silent no-rollback deploy.
    """
    responded, build_sha = probe_version()
    if not responded:
        return TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE, None
    running_digest = _running_digest(executor, cfg)
    target = select_rollback_target(
        runtime_build_sha=build_sha,
        running_digest=running_digest,
        legacy=cfg.legacy_artifact,
        version_responded=True,
    )
    if target is not None:
        return None, target
    if build_sha is None and cfg.legacy_artifact is None:
        return TransportOutcome.LEGACY_ROLLBACK_ARTIFACT_REQUIRED, None
    return TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE, None


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
    try:
        resolve_release_path(cfg.deploy_root, cfg.target_sha)
    except ReleasePathError:
        return TransportOutcome.RELEASE_PATH_INVALID
    return None


def _rollback(
    executor: RemoteExecutor,
    cfg: DeployConfig,
    target: RollbackTarget,
    verify: Callable[[str], VerifyResult],
    verify_legacy: Callable[[str], bool],
    build_audit: Callable[..., DeploymentAudit],
) -> DeploymentAudit:
    """Attempt a single immutable rollback (no loop) to the same Compose authority.

    Uses the exact captured previous digest, verifying normally (build_sha) or, for
    a first-deploy legacy target, by exact digest + health/startup/readiness (never
    SHA). Never switches to the legacy host Compose file.
    """
    plan = plan_rollback(
        previous_artifact=target.image_digest,
        restart_required=True,
        dhan_restart_safety_confirmed=cfg.dhan_restart_safe,
    )
    if plan.decision is RollbackDecision.REQUIRES_OPERATOR_INTERVENTION:
        return build_audit(
            TransportOutcome.ROLLBACK_REQUIRES_OPERATOR_INTERVENTION, False, "dhan_unsafe"
        )
    if plan.decision is RollbackDecision.UNAVAILABLE:
        return build_audit(TransportOutcome.ROLLBACK_TARGET_UNAVAILABLE, False, None)
    if not _pull(executor, cfg, target.image_digest):
        return build_audit(TransportOutcome.ROLLBACK_FAILED, True, "pull_failed")
    if not _update_backend(executor, cfg, target.image_digest):
        return build_audit(TransportOutcome.ROLLBACK_FAILED, True, "update_failed")
    if target.legacy:
        if verify_legacy(target.image_digest):
            return build_audit(TransportOutcome.LEGACY_ROLLED_BACK, True, "legacy_success")
        return build_audit(TransportOutcome.ROLLBACK_FAILED, True, "legacy_verification_failed")
    verified = verify(target.source_sha)
    if verified.outcome is VerifyOutcome.SUCCESS:
        return build_audit(TransportOutcome.ROLLED_BACK, True, "success")
    return build_audit(TransportOutcome.ROLLBACK_FAILED, True, verified.outcome.value)


def _pre_mutation_gate(
    executor: RemoteExecutor,
    cfg: DeployConfig,
    probe_version: Callable[[], tuple[bool, str | None]],
) -> tuple[TransportOutcome | None, RollbackTarget | None]:
    """Run every pre-mutation gate in order; return (failure_or_None, target_or_None).

    Ordering: validate -> preflight -> resolve rollback target -> pull. Rollback
    eligibility is proven BEFORE any mutation; on pull failure the resolved target
    is still returned for the audit. No mutation occurs in any failing path.
    """
    invalid = _validate(cfg)
    if invalid is not None:
        return invalid, None
    preflight_failure = _preflight(executor, cfg)
    if preflight_failure is not None:
        return preflight_failure, None
    rollback_failure, target = _resolve_rollback(executor, cfg, probe_version)
    if rollback_failure is not None:
        return rollback_failure, None
    if not _pull(executor, cfg, cfg.target_image):
        return TransportOutcome.TARGET_IMAGE_PULL_FAILED, target
    return None, target


def deploy(
    executor: RemoteExecutor,
    cfg: DeployConfig,
    *,
    verify: Callable[[str], VerifyResult],
    verify_legacy: Callable[[str], bool],
    probe_version: Callable[[], tuple[bool, str | None]],
    now: Callable[[], float] = time.time,
) -> DeploymentAudit:
    """Promote ``cfg.target_image`` to production, rolling back on any failure.

    Enforces the ordering: validate -> preflight -> resolve rollback target ->
    pull -> mutate -> verify, and on post-mutation failure -> single rollback ->
    verify. No mutation occurs until every pre-mutation gate passes (including
    proving a rollback target exists); fails closed.
    """
    started = now()
    failure, target = _pre_mutation_gate(executor, cfg, probe_version)

    def audit(
        outcome: TransportOutcome,
        *,
        verification: str = "not_started",
        rollback_attempted: bool = False,
        rollback_result: str | None = None,
    ) -> DeploymentAudit:
        return DeploymentAudit(
            run_id=cfg.run_id,
            requested_sha=cfg.target_sha,
            target_digest=cfg.target_image,
            previous_sha=target.source_sha if target else None,
            previous_digest=target.image_digest if target else None,
            started_at=started,
            completed_at=now(),
            verification=verification,
            rollback_attempted=rollback_attempted,
            rollback_result=rollback_result,
            outcome=outcome,
        )

    if failure is not None:
        return audit(failure)
    assert target is not None  # gate guarantees it on the success path

    def do_rollback(failure_reason: str) -> DeploymentAudit:
        return _rollback(
            executor,
            cfg,
            target,
            verify,
            verify_legacy,
            lambda o, a, r: audit(
                o, verification=failure_reason, rollback_attempted=a, rollback_result=r
            ),
        )

    if not _update_backend(executor, cfg, cfg.target_image):
        return do_rollback("deployment_command_failed")
    result = verify(cfg.target_sha)
    if result.outcome is VerifyOutcome.SUCCESS:
        return audit(TransportOutcome.SUCCESS, verification="success")
    return do_rollback(_VERIFY_TO_OUTCOME[result.outcome].value)


def _build_verifier(
    base_url: str, *, attempts: int, interval: float, timeout: float
) -> tuple[Callable[[str], VerifyResult], Callable[[], tuple[bool, str | None]]]:
    """Wire the HTTP probes into a (verify, probe_version) pair for real deploys."""
    from deploy.health_check import probe_endpoints, probe_version, verify_release

    root = base_url.rstrip("/")

    def verify(expected_sha: str) -> VerifyResult:
        return verify_release(
            lambda: probe_endpoints(root, timeout),
            expected_sha=expected_sha,
            max_attempts=attempts,
            sleep=lambda: time.sleep(interval),
        )

    def version() -> tuple[bool, str | None]:
        return probe_version(root, timeout)

    return verify, version


def _build_legacy_verifier(
    executor: RemoteExecutor, cfg: DeployConfig, *, timeout: float
) -> Callable[[str], bool]:
    """Legacy rollback verifier: exact running digest + health (never SHA)."""
    from deploy.health_check import probe_endpoints

    def verify_legacy(expected_digest: str) -> bool:
        probe = probe_endpoints(cfg.base_url.rstrip("/"), timeout)
        return legacy_rollback_verified(
            running_digest=_running_digest(executor, cfg) or "",
            expected_digest=expected_digest,
            health_ok=probe.health_ok,
            startup_ok=probe.startup_ok,
            ready_ok=probe.ready_ok,
        )

    return verify_legacy


def _legacy_artifact_from_args(args: object) -> LegacyRollbackArtifact | None:
    """Build a LegacyRollbackArtifact from optional CLI args (fail closed on invalid)."""
    digest = args.legacy_digest  # type: ignore[attr-defined]
    if not digest:
        return None
    return LegacyRollbackArtifact(
        image_digest=digest,
        running_image_id=args.legacy_image_id,  # type: ignore[attr-defined]
        source_sha_evidence=args.legacy_source_sha,  # type: ignore[attr-defined]
        source_sha_evidence_kind=SourceShaEvidenceKind(args.legacy_evidence_kind),  # type: ignore[attr-defined]
        provenance=args.legacy_provenance,  # type: ignore[attr-defined]
    )


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
    parser.add_argument("--deploy-root", required=True, help="Versioned release root dir.")
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
    parser.add_argument("--legacy-digest", default="", help="First-deploy legacy rollback digest.")
    parser.add_argument("--legacy-image-id", default="", help="Legacy running image id evidence.")
    parser.add_argument("--legacy-source-sha", default="", help="Legacy provenance SHA.")
    parser.add_argument(
        "--legacy-evidence-kind",
        choices=[k.value for k in SourceShaEvidenceKind],
        default=SourceShaEvidenceKind.IMAGE_TAG.value,
    )
    parser.add_argument("--legacy-provenance", default="operator-provisioned")
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
    try:
        legacy_artifact = _legacy_artifact_from_args(args)
    except LegacyArtifactError as error:
        print(f"outcome=legacy_rollback_artifact_required detail={error}", file=sys.stderr)
        return 1
    cfg = DeployConfig(
        target_image=args.image,  # type: ignore[attr-defined]
        target_sha=args.target_sha,  # type: ignore[attr-defined]
        deploy_root=args.deploy_root,  # type: ignore[attr-defined]
        base_url=args.base_url,  # type: ignore[attr-defined]
        dhan_restart_safe=args.dhan_restart_safe,  # type: ignore[attr-defined]
        require_migrations=args.require_migrations,  # type: ignore[attr-defined]
        run_id=args.run_id,  # type: ignore[attr-defined]
        docker_privilege=DockerPrivilege(args.docker_privilege),  # type: ignore[attr-defined]
        project_name=args.project_name,  # type: ignore[attr-defined]
        legacy_artifact=legacy_artifact,
    )
    verify, probe_version = _build_verifier(
        cfg.base_url,
        attempts=args.attempts,  # type: ignore[attr-defined]
        interval=args.interval,  # type: ignore[attr-defined]
        timeout=args.timeout,  # type: ignore[attr-defined]
    )
    verify_legacy = _build_legacy_verifier(executor, cfg, timeout=args.timeout)  # type: ignore[attr-defined]
    audit = deploy(
        executor, cfg, verify=verify, verify_legacy=verify_legacy, probe_version=probe_version
    )
    print(
        f"outcome={audit.outcome.value} verification={audit.verification} "
        f"rollback_attempted={audit.rollback_attempted} rollback_result={audit.rollback_result} "
        f"requested_sha={audit.requested_sha} previous_sha={audit.previous_sha}"
    )
    return 0 if audit.outcome is TransportOutcome.SUCCESS else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
