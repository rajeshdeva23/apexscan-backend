"""Stage a verified deployment bundle into a versioned production release dir (DEPLOY-3C).

Closes the gap between the built ``deployment-bundle-<sha>`` artifact and the release
directory ``deploy.transport`` expects. It verifies the bundle with the existing
``deploy.bundle.verify_bundle`` (fail closed on tamper/traversal/symlink/SHA mismatch),
then over a hardened :class:`~deploy.executor.RemoteExecutor` places ONLY
``docker-compose.production.yml`` at ``<deploy_root>/<sha>/`` — atomically and
idempotently — and re-reads it to confirm the bytes landed.

It never pulls, never runs ``docker compose up``, never restarts/recreates a container,
never touches env/secrets/Dhan, and never prunes or modifies a sibling release. Path
resolution reuses ``deploy.transport.resolve_release_path`` (the same validator the
transport preflight enforces), so a successfully staged release satisfies that preflight.
Pure orchestration over the injected executor — fully exercised offline with a fake.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from deploy.bundle import PRODUCTION_COMPOSE_FILE, BundleError, verify_bundle
from deploy.executor import RemoteExecutor
from deploy.transport import DockerPrivilege, ReleasePathError, resolve_release_path

logger = logging.getLogger(__name__)

# The release directory may contain ONLY the production Compose authority. Secrets and
# durable state stay external (/etc/apexscan, named volumes); the bundle carries no more.
_ALLOWED_FILES = frozenset({PRODUCTION_COMPOSE_FILE})

# Fixed, closed privilege prefixes — never a caller-supplied prefix string.
_PRIVILEGE_PREFIX: dict[DockerPrivilege, tuple[str, ...]] = {
    DockerPrivilege.DIRECT: (),
    DockerPrivilege.SUDO_NON_INTERACTIVE: ("sudo", "-n"),
}

# Atomic host write with NO interpolation of dynamic data into the script text: the
# release dir, base64 payload, and filename arrive as positional args ($1/$2/$3), so
# even a hostile value cannot break out of the shell. Written to a temp file in the SAME
# directory, then renamed (atomic on one filesystem) so a partial transfer never replaces
# a valid release. No mkdir/pull/up here — placement only.
_WRITE_SCRIPT = (
    'set -eu; d="$1"; b="$2"; f="$3"; umask 077; '
    't="$d/.stage.$$.tmp"; '
    'printf %s "$b" | base64 -d > "$t"; '
    'mv -f "$t" "$d/$f"'
)


class StageOutcome(StrEnum):
    """Terminal outcome of a staging attempt (all failures are fail-closed, no mutation loop)."""

    STAGED = "staged"
    ALREADY_STAGED = "already_staged"
    RELEASE_PATH_INVALID = "release_path_invalid"
    BUNDLE_INVALID = "bundle_invalid"
    BUNDLE_UNEXPECTED_CONTENT = "bundle_unexpected_content"
    RELEASE_CONFLICT = "release_conflict"
    SSH_UNREACHABLE = "ssh_unreachable"
    PRIVILEGE_UNAVAILABLE = "privilege_unavailable"
    MKDIR_FAILED = "mkdir_failed"
    WRITE_FAILED = "write_failed"
    VERIFY_FAILED = "verify_failed"


@dataclass(frozen=True, slots=True)
class StageResult:
    """Sanitized staging result (no bundle bytes, no secrets, no payload)."""

    outcome: StageOutcome
    target_sha: str
    release_path: str | None
    staged_file: str | None
    changed: bool


@dataclass(frozen=True, slots=True)
class StageConfig:
    """Inputs describing one release-staging (no secrets; SSH lives in the executor)."""

    deploy_root: str
    target_sha: str
    docker_privilege: DockerPrivilege = DockerPrivilege.DIRECT
    compose_file: str = PRODUCTION_COMPOSE_FILE


def _priv(cfg: StageConfig, *args: str) -> list[str]:
    """Prefix a host command with the configured (closed) privilege escalation."""
    return [*_PRIVILEGE_PREFIX[cfg.docker_privilege], *args]


def _remote_sha256(executor: RemoteExecutor, cfg: StageConfig, path: str) -> str | None:
    """Return the remote file's sha256 hex, or ``None`` if it is absent/unreadable."""
    result = executor.run(_priv(cfg, "sha256sum", path))
    if not result.ok:
        return None
    token = result.stdout.split()
    return token[0] if token else None


def _preflight(executor: RemoteExecutor, cfg: StageConfig) -> StageOutcome | None:
    """Prove the host is reachable and privilege is usable; else a fail-closed outcome."""
    if not executor.run(["true"]).ok:
        return StageOutcome.SSH_UNREACHABLE
    if (
        cfg.docker_privilege is DockerPrivilege.SUDO_NON_INTERACTIVE
        and not executor.run(["sudo", "-n", "true"]).ok
    ):
        return StageOutcome.PRIVILEGE_UNAVAILABLE
    return None


def _verified_compose(bundle: bytes, cfg: StageConfig) -> tuple[bytes | None, StageOutcome | None]:
    """Verify the bundle and return the single compose file's bytes (fail closed)."""
    try:
        files = verify_bundle(bundle, expected_sha=cfg.target_sha)
    except BundleError:
        return None, StageOutcome.BUNDLE_INVALID
    if set(files) != _ALLOWED_FILES or cfg.compose_file not in files:
        return None, StageOutcome.BUNDLE_UNEXPECTED_CONTENT
    return files[cfg.compose_file], None


def stage(executor: RemoteExecutor, cfg: StageConfig, *, bundle: bytes) -> StageResult:
    """Verify ``bundle`` for ``cfg.target_sha`` and stage its compose file to the release dir.

    Ordering (fail closed, no mutation until every gate passes): verify bundle -> resolve
    release path -> reachability/privilege preflight -> idempotency/conflict check ->
    mkdir -> atomic write -> re-verify. Verification runs BEFORE any host command, so a
    bad bundle never touches the host. Returns a sanitized :class:`StageResult`.
    """
    content, bundle_failure = _verified_compose(bundle, cfg)
    if bundle_failure is not None:
        return _result(bundle_failure, cfg, None, changed=False)
    assert content is not None

    try:
        release_dir = resolve_release_path(cfg.deploy_root, cfg.target_sha)
    except ReleasePathError:
        return _result(StageOutcome.RELEASE_PATH_INVALID, cfg, None, changed=False)
    release_file = f"{release_dir}/{cfg.compose_file}"

    preflight_failure = _preflight(executor, cfg)
    if preflight_failure is not None:
        return _result(preflight_failure, cfg, release_file, changed=False)

    return _place(executor, cfg, content, release_dir, release_file)


def _place(
    executor: RemoteExecutor,
    cfg: StageConfig,
    content: bytes,
    release_dir: str,
    release_file: str,
) -> StageResult:
    """Idempotently place the compose file: skip if identical, fail closed if it differs."""
    expected_hash = hashlib.sha256(content).hexdigest()
    if executor.run(_priv(cfg, "test", "-f", release_file)).ok:
        existing = _remote_sha256(executor, cfg, release_file)
        if existing == expected_hash:
            return _result(StageOutcome.ALREADY_STAGED, cfg, release_file, changed=False)
        return _result(StageOutcome.RELEASE_CONFLICT, cfg, release_file, changed=False)

    if not executor.run(_priv(cfg, "mkdir", "-p", release_dir)).ok:
        return _result(StageOutcome.MKDIR_FAILED, cfg, release_file, changed=False)

    payload = base64.b64encode(content).decode("ascii")
    write = executor.run(
        _priv(cfg, "sh", "-c", _WRITE_SCRIPT, "sh", release_dir, payload, cfg.compose_file)
    )
    if not write.ok:
        return _result(StageOutcome.WRITE_FAILED, cfg, release_file, changed=False)

    if _remote_sha256(executor, cfg, release_file) != expected_hash:
        return _result(StageOutcome.VERIFY_FAILED, cfg, release_file, changed=False)
    return _result(StageOutcome.STAGED, cfg, release_file, changed=True)


def _result(
    outcome: StageOutcome, cfg: StageConfig, release_file: str | None, *, changed: bool
) -> StageResult:
    """Build a sanitized result and log one structured line (no payload/secret)."""
    result = StageResult(
        outcome=outcome,
        target_sha=cfg.target_sha,
        release_path=release_file,
        staged_file=release_file if outcome in _PLACED else None,
        changed=changed,
    )
    logger.info(
        "RELEASE_STAGING outcome=%s target_sha=%s release_path=%s changed=%s",
        result.outcome.value,
        result.target_sha,
        result.release_path or "none",
        result.changed,
    )
    return result


_PLACED = frozenset({StageOutcome.STAGED, StageOutcome.ALREADY_STAGED})


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: verify a bundle and stage its production compose file over hardened SSH."""
    import argparse
    from pathlib import Path

    from deploy.executor import SSHExecutor

    parser = argparse.ArgumentParser(description="Stage a verified deploy bundle to a release dir.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--deploy-root", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--bundle", required=True, help="Path to the deployment bundle .tar.gz")
    parser.add_argument(
        "--docker-privilege",
        choices=[p.value for p in DockerPrivilege],
        default=DockerPrivilege.SUDO_NON_INTERACTIVE.value,
    )
    args = parser.parse_args(argv)

    executor = SSHExecutor(
        host=args.host,
        user=args.user,
        key_file=args.key_file,
        known_hosts_file=args.known_hosts,
    )
    cfg = StageConfig(
        deploy_root=args.deploy_root,
        target_sha=args.target_sha,
        docker_privilege=DockerPrivilege(args.docker_privilege),
    )
    result = stage(executor, cfg, bundle=Path(args.bundle).read_bytes())
    print(
        "RELEASE_STAGING "
        f"outcome={result.outcome.value} target_sha={result.target_sha} "
        f"release_path={result.release_path or 'none'} changed={result.changed}"
    )
    return 0 if result.outcome in _PLACED else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
