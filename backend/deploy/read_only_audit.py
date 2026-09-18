"""Strictly read-only production state audit (PROD-AUDIT).

Reports SANITIZED production state over the SAME hardened SSH transport the deploy
pipeline uses (:class:`deploy.executor.SSHExecutor`), plus the existing read-only
``GET /api/v1/version`` probe. It NEVER mutates: no ``docker pull/up/down/restart/
stop/start/rm``, no ``exec``, no ``git``, no ``systemctl``, no Redis/DB/Dhan I/O.
The host commands are a FIXED, closed allow-list of inspection-only argv (never
caller-supplied), so a static test can prove no mutating verb is reachable.

Secret safety: it never dumps environment (`env`/`printenv`), never ``cat``s an
env file, and never reads a container's environment array. Configuration is
retrieved by grepping an explicit ALLOW-LIST of non-secret keys out of the
external env files and sanitizing each value to ``ON``/``OFF``/``MISSING``/
``UNKNOWN`` (or a bare integer for the lease timings). No credential is ever
read or printed.

Like the rest of ``deploy``, the transport is injected so the whole projection is
exercised offline with fakes; ``main`` wires the real SSH executor + HTTP probe.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from deploy.executor import ExecResult, RemoteExecutor, SSHExecutor

# Expected repository revisions this audit compares the live build against. Short
# SHAs are compared by prefix against the full 40-char build_sha from /version.
DEFAULT_MAIN_SHA = "a6b8c68"
DEFAULT_DECOUPLING_SHA = "6c52cd8"

# Container names from the production Compose authority (docker-compose.production.yml).
_BACKEND = "apexscan-backend"
_INGESTION = "apexscan-market-ingestion"
_REDIS = "apexscan-redis"
_POSTGRES = "apexscan-postgres"

# Allow-listed, NON-SECRET configuration keys. Booleans are sanitized to ON/OFF;
# the two timings are reported as bare integers. Nothing here is a credential.
_FLAG_KEYS: tuple[str, ...] = (
    "MARKET_INGESTION_SERVICE_ENABLED",
    "MARKET_OWNERSHIP_ENABLED",
    "IPC_AUTHORITATIVE_ENABLED",
)
_TIMING_KEYS: tuple[str, ...] = (
    "MARKET_OWNERSHIP_LEASE_TTL_SECONDS",
    "MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS",
)
# The only env files the deployment references (docker-compose.production.yml env_file).
# Listed explicitly (never a `*.env` glob) so no shell expansion is relied upon.
_ENV_FILES: tuple[str, ...] = (
    "/etc/apexscan/apexscan-infra.env",
    "/etc/apexscan/backend.env",
    "/etc/apexscan/dhan.env",
)
# Anchored allow-list regex: only lines assigning one of the allow-listed keys match.
_ALLOWLIST_RE = rf"^[[:space:]]*({'|'.join((*_FLAG_KEYS, *_TIMING_KEYS))})="

# Flags whose truthiness means authority is ACTIVE — either being ON triggers a stop.
_AUTHORITY_STOP_FLAGS: tuple[str, ...] = (
    "MARKET_OWNERSHIP_ENABLED",
    "IPC_AUTHORITATIVE_ENABLED",
)

_FALSY = frozenset({"0", "false", "f", "no", "n", "off", ""})
_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


class Flag(StrEnum):
    """Sanitized state of an allow-listed configuration flag."""

    ON = "ON"
    OFF = "OFF"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class Tri(StrEnum):
    """Three-valued answer where a fact may be unprovable."""

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"
    CANNOT_PROVE = "CANNOT_PROVE"


class DockerPrivilege(StrEnum):
    """How host commands are invoked (closed set — never arbitrary shell)."""

    DIRECT = "direct"
    SUDO_NON_INTERACTIVE = "sudo_non_interactive"


_PRIVILEGE_PREFIX: dict[DockerPrivilege, tuple[str, ...]] = {
    DockerPrivilege.DIRECT: (),
    DockerPrivilege.SUDO_NON_INTERACTIVE: ("sudo", "-n"),
}


class AuditError(RuntimeError):
    """Raised when the transport is unusable and no audit can be produced (fail closed)."""


@dataclass(frozen=True, slots=True)
class ContainerRaw:
    """Raw inspection of one container (present? running? image reference)."""

    exists: bool
    running: bool | None = None
    image: str | None = None
    health: str | None = None


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """Everything collected from the host + the /version probe, pre-sanitization."""

    backend: ContainerRaw
    ingestion: ContainerRaw
    redis: ContainerRaw
    postgres: ContainerRaw
    flag_lines: str
    version_responded: bool
    backend_build_sha: str | None


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Fully sanitized, secret-free production audit."""

    backend_running: Tri
    backend_build_sha: str
    backend_image: str
    ingestion_exists: Tri
    ingestion_running: Tri
    ingestion_build_sha: str
    ingestion_image: str
    redis_status: str
    postgres_status: str
    flags: dict[str, Flag]
    ownership_ttl: str
    ownership_renewal: str
    backend_ingestion_revision_parity: Tri
    main_deployed: Tri
    decoupling_deployed: Tri
    safety_stop: bool


def _priv(privilege: DockerPrivilege, *args: str) -> list[str]:
    """Prefix a host command with the configured (closed) privilege escalation."""
    return [*_PRIVILEGE_PREFIX[privilege], *args]


def _inspect_container(privilege: DockerPrivilege, name: str) -> list[str]:
    r"""Read-only ``docker inspect`` for one container's running/image/health facts.

    Reads only ``.State`` and ``.Config.Image`` — never ``.Config.Env`` (the
    environment array is off-limits). Returns tab-separated ``running\timage\thealth``.
    """
    fmt = (
        "{{.State.Running}}\t{{.Config.Image}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}"
    )
    return _priv(privilege, "docker", "inspect", "--format", fmt, name)


def _grep_flags(privilege: DockerPrivilege) -> list[str]:
    """Grep ONLY the allow-listed config keys out of the explicit env files.

    ``-h`` drops filenames, ``-s`` suppresses missing-file errors, ``-E`` enables
    the anchored allow-list regex. This is a targeted key extraction, not a dump:
    only lines assigning an allow-listed (non-secret) key can ever match.
    """
    return _priv(privilege, "grep", "-hsE", _ALLOWLIST_RE, *_ENV_FILES)


def read_only_commands(privilege: DockerPrivilege) -> dict[str, list[str]]:
    """The complete, fixed set of host commands this audit may run (all read-only)."""
    return {
        "backend": _inspect_container(privilege, _BACKEND),
        "ingestion": _inspect_container(privilege, _INGESTION),
        "redis": _inspect_container(privilege, _REDIS),
        "postgres": _inspect_container(privilege, _POSTGRES),
        "flags": _grep_flags(privilege),
    }


def _parse_container(result: ExecResult) -> ContainerRaw:
    """Parse a ``docker inspect`` result into a :class:`ContainerRaw` (absent on nonzero)."""
    if not result.ok:
        return ContainerRaw(exists=False)
    parts = result.stdout.strip().split("\t")
    running = parts[0].strip().lower() == "true" if parts and parts[0] else None
    image = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    health = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    return ContainerRaw(exists=True, running=running, image=image, health=health)


def _preflight(executor: RemoteExecutor, privilege: DockerPrivilege) -> None:
    """Fail closed if the host is unreachable, SSH is unverified, or sudo is unusable."""
    reachable = executor.run(["true"])
    if not reachable.ok:
        text = reachable.stderr.lower()
        if "host key" in text or "known_hosts" in text or "verification failed" in text:
            raise AuditError("SSH_HOST_VERIFICATION_FAILED")
        raise AuditError("SSH_CONNECTION_FAILED")
    if (
        privilege is DockerPrivilege.SUDO_NON_INTERACTIVE
        and not executor.run(["sudo", "-n", "true"]).ok
    ):
        raise AuditError("DOCKER_PRIVILEGE_UNAVAILABLE")


def collect(
    executor: RemoteExecutor,
    probe_version: Callable[[], tuple[bool, str | None]],
    *,
    privilege: DockerPrivilege = DockerPrivilege.DIRECT,
) -> RawSnapshot:
    """Run the fixed read-only command set + version probe into a raw snapshot."""
    _preflight(executor, privilege)
    commands = read_only_commands(privilege)
    names = ("backend", "ingestion", "redis", "postgres")
    containers = {name: executor.run(commands[name]) for name in names}
    flags = executor.run(commands["flags"])
    responded, build_sha = probe_version()
    return RawSnapshot(
        backend=_parse_container(containers["backend"]),
        ingestion=_parse_container(containers["ingestion"]),
        redis=_parse_container(containers["redis"]),
        postgres=_parse_container(containers["postgres"]),
        flag_lines=flags.stdout if flags.ok else "",
        version_responded=responded,
        backend_build_sha=build_sha,
    )


def _collapse(values: list[str]) -> str | None:
    """Collapse repeated key values: single -> that value; conflicting -> None (UNKNOWN)."""
    distinct = {v for v in values}
    if not distinct:
        return None
    if len(distinct) > 1:
        return None
    return next(iter(distinct))


def parse_config_lines(raw: str) -> dict[str, str | None]:
    """Parse allow-listed ``KEY=VALUE`` lines into a map (conflicting dupes -> None).

    Absent keys are omitted (caller renders them MISSING); a key present with
    conflicting values across files maps to None (rendered UNKNOWN). Surrounding
    quotes and inline whitespace are stripped; values are otherwise untouched.
    """
    collected: dict[str, list[str]] = {}
    allowed = set((*_FLAG_KEYS, *_TIMING_KEYS))
    for line in raw.splitlines():
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip().strip("'\"")
        collected.setdefault(key, []).append(value)
    return {key: _collapse(values) for key, values in collected.items()}


def _sanitize_flag(config: dict[str, str | None], key: str) -> Flag:
    """Map a raw flag value to ON/OFF/MISSING/UNKNOWN (fail toward UNKNOWN, not ON)."""
    if key not in config:
        return Flag.MISSING
    value = config[key]
    if value is None:
        return Flag.UNKNOWN
    lowered = value.lower()
    if lowered in _TRUTHY:
        return Flag.ON
    if lowered in _FALSY:
        return Flag.OFF
    return Flag.UNKNOWN


def _sanitize_timing(config: dict[str, str | None], key: str) -> str:
    """Map a raw timing value to a bare integer string, else MISSING/UNKNOWN."""
    if key not in config:
        return Flag.MISSING.value
    value = config[key]
    if value is None or not value.isdigit():
        return Flag.UNKNOWN.value
    return value


def _tri_running(container: ContainerRaw) -> Tri:
    """YES/NO/UNKNOWN for whether a present container is running."""
    if not container.exists or container.running is None:
        return Tri.UNKNOWN if container.exists else Tri.NO
    return Tri.YES if container.running else Tri.NO


def _status(container: ContainerRaw) -> str:
    """Human-readable status token for an infra container (redis/postgres)."""
    if not container.exists:
        return "absent"
    if container.running is None:
        return "unknown"
    state = "running" if container.running else "stopped"
    return f"{state}:{container.health}" if container.health else state


def _sha_matches(build_sha: str | None, expected: str) -> Tri:
    """Whether the live build_sha corresponds to an expected (possibly short) SHA."""
    if not build_sha:
        return Tri.CANNOT_PROVE
    live = build_sha.strip().lower()
    want = expected.strip().lower()
    return Tri.YES if live.startswith(want) or want.startswith(live) else Tri.NO


def _parity(backend: ContainerRaw, ingestion: ContainerRaw) -> Tri:
    """Whether both application services run the identical immutable image."""
    if not backend.image or not ingestion.image:
        return Tri.CANNOT_PROVE
    return Tri.YES if backend.image == ingestion.image else Tri.NO


def project_audit(
    snapshot: RawSnapshot,
    *,
    main_sha: str = DEFAULT_MAIN_SHA,
    decoupling_sha: str = DEFAULT_DECOUPLING_SHA,
) -> AuditReport:
    """Project a raw snapshot into the fully sanitized, secret-free audit report."""
    config = parse_config_lines(snapshot.flag_lines)
    flags = {key: _sanitize_flag(config, key) for key in _FLAG_KEYS}
    parity = _parity(snapshot.backend, snapshot.ingestion)
    build_sha = snapshot.backend_build_sha
    # Digest identity ⇒ same baked BUILD_SHA, so a proven-equal image lets us report
    # the ingestion revision; otherwise it is not independently obtainable (no HTTP port).
    ingestion_sha = build_sha if parity is Tri.YES and build_sha else Flag.UNKNOWN.value
    return AuditReport(
        backend_running=_tri_running(snapshot.backend),
        backend_build_sha=build_sha or Flag.UNKNOWN.value,
        backend_image=snapshot.backend.image or Flag.UNKNOWN.value,
        ingestion_exists=Tri.YES if snapshot.ingestion.exists else Tri.NO,
        ingestion_running=_tri_running(snapshot.ingestion),
        ingestion_build_sha=ingestion_sha,
        ingestion_image=snapshot.ingestion.image or Flag.UNKNOWN.value,
        redis_status=_status(snapshot.redis),
        postgres_status=_status(snapshot.postgres),
        flags=flags,
        ownership_ttl=_sanitize_timing(config, "MARKET_OWNERSHIP_LEASE_TTL_SECONDS"),
        ownership_renewal=_sanitize_timing(config, "MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS"),
        backend_ingestion_revision_parity=parity,
        main_deployed=_sha_matches(build_sha, main_sha),
        decoupling_deployed=_sha_matches(build_sha, decoupling_sha),
        safety_stop=any(flags[key] is Flag.ON for key in _AUTHORITY_STOP_FLAGS),
    )


def render(report: AuditReport) -> str:
    """Render the sanitized report as the fixed ``PRODUCTION_READ_ONLY_AUDIT`` block."""
    lines = [
        "PRODUCTION_READ_ONLY_AUDIT",
        "",
        f"backend_running={report.backend_running.value}",
        f"backend_build_sha={report.backend_build_sha}",
        f"backend_image={report.backend_image}",
        "",
        f"ingestion_exists={report.ingestion_exists.value}",
        f"ingestion_running={report.ingestion_running.value}",
        f"ingestion_build_sha={report.ingestion_build_sha}",
        f"ingestion_image={report.ingestion_image}",
        "",
        f"redis_status={report.redis_status}",
        f"postgres_status={report.postgres_status}",
        "",
        f"market_ingestion_service_enabled={report.flags['MARKET_INGESTION_SERVICE_ENABLED'].value}",
        f"market_ownership_enabled={report.flags['MARKET_OWNERSHIP_ENABLED'].value}",
        f"ipc_authoritative_enabled={report.flags['IPC_AUTHORITATIVE_ENABLED'].value}",
        "",
        f"ownership_ttl={report.ownership_ttl}",
        f"ownership_renewal={report.ownership_renewal}",
        "",
        f"backend_ingestion_revision_parity={report.backend_ingestion_revision_parity.value}",
        "",
        f"main_a6b8c68_deployed={report.main_deployed.value}",
        f"decoupling_6c52cd8_deployed={report.decoupling_deployed.value}",
        "",
        f"safety_stop={'TRUE' if report.safety_stop else 'FALSE'}",
    ]
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the audit CLI arguments (mirrors the transport's SSH wiring)."""
    parser = argparse.ArgumentParser(description="Strictly read-only production state audit.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--base-url", required=True, help="Base URL for the /version probe.")
    parser.add_argument(
        "--docker-privilege",
        choices=[p.value for p in DockerPrivilege],
        default=DockerPrivilege.DIRECT.value,
        help="How host commands run (production ubuntu needs sudo_non_interactive).",
    )
    parser.add_argument("--main-sha", default=DEFAULT_MAIN_SHA)
    parser.add_argument("--decoupling-sha", default=DEFAULT_DECOUPLING_SHA)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: run the read-only audit over SSH and print the sanitized block (fail closed)."""
    from deploy.health_check import probe_version

    args = _parse_args(argv)
    executor = SSHExecutor(
        host=args.host,
        user=args.user,
        key_file=args.key_file,
        known_hosts_file=args.known_hosts,
    )
    base = args.base_url.rstrip("/")
    try:
        snapshot = collect(
            executor,
            lambda: probe_version(base, args.timeout),
            privilege=DockerPrivilege(args.docker_privilege),
        )
    except AuditError as error:
        print(f"AUDIT_FAILED: {error}", file=sys.stderr)
        return 1
    report = project_audit(snapshot, main_sha=args.main_sha, decoupling_sha=args.decoupling_sha)
    print(render(report))
    if report.safety_stop:
        print("SAFETY_STOP: authority flag ON in production (not changed).", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
