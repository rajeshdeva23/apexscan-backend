"""Offline tests for the strictly read-only production audit (PROD-AUDIT).

Prove: the host command set is inspection-only (no mutating verb, no env dump),
allow-listed config values sanitize to ON/OFF/MISSING/UNKNOWN, secrets never
surface, the SAFETY_STOP fires when an authority flag is ON, revision comparison
against the expected SHAs, and fail-closed on an unreachable/unverified host.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from deploy.executor import ExecResult
from deploy.read_only_audit import (
    DEFAULT_DECOUPLING_SHA,
    DEFAULT_MAIN_SHA,
    AuditError,
    DockerPrivilege,
    Flag,
    Tri,
    collect,
    parse_config_lines,
    project_audit,
    read_only_commands,
    render,
)

_MAIN_FULL = DEFAULT_MAIN_SHA + "0" * (40 - len(DEFAULT_MAIN_SHA))
_DECOUPLING_FULL = DEFAULT_DECOUPLING_SHA + "1" * (40 - len(DEFAULT_DECOUPLING_SHA))
_IMAGE = "ghcr.io/o/apexscan-backend@sha256:" + "a" * 64
_OTHER_IMAGE = "ghcr.io/o/apexscan-backend@sha256:" + "b" * 64

# Verbs that mutate host/docker/git/service state — none may appear in any audit command.
_MUTATING = {
    "pull",
    "push",
    "up",
    "down",
    "restart",
    "stop",
    "start",
    "rm",
    "rmi",
    "create",
    "exec",
    "kill",
    "prune",
    "build",
    "run",
    "commit",
    "cp",
    "tag",
    "load",
    "save",
    "login",
    "logout",
    "checkout",
    "reset",
    "systemctl",
    "cat",
    "env",
    "printenv",
    "redis-cli",
    "psql",
    "tee",
    "sed",
}


class FakeExecutor:
    """Records commands and returns programmed results (default: success)."""

    def __init__(self, handler: Callable[[list[str]], ExecResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._handler = handler or (lambda _cmd: ExecResult(0, "", ""))

    def run(self, command: Sequence[str], *, timeout: float | None = None) -> ExecResult:
        cmd = list(command)
        self.calls.append(cmd)
        return self._handler(cmd)


def _version(responded: bool, sha: str | None) -> Callable[[], tuple[bool, str | None]]:
    return lambda: (responded, sha)


# --- static safety: the command set is inspection-only ----------------------------------------


@pytest.mark.parametrize("privilege", list(DockerPrivilege))
def test_no_command_contains_a_mutating_verb(privilege: DockerPrivilege) -> None:
    for cmd in read_only_commands(privilege).values():
        assert not (_MUTATING & set(cmd)), f"mutating token in {cmd}"


def test_commands_are_inspection_only_docker_and_grep() -> None:
    cmds = read_only_commands(DockerPrivilege.DIRECT)
    for name in ("backend", "ingestion", "redis", "postgres"):
        assert cmds[name][:2] == ["docker", "inspect"]
    assert cmds["flags"][0] == "grep"
    # never reads the container environment array, never cats an env file.
    assert not any(".Config.Env" in tok for cmd in cmds.values() for tok in cmd)


def test_sudo_privilege_prefixes_every_host_command() -> None:
    for cmd in read_only_commands(DockerPrivilege.SUDO_NON_INTERACTIVE).values():
        assert cmd[:2] == ["sudo", "-n"]


def test_grep_only_matches_allow_listed_keys() -> None:
    pattern = read_only_commands(DockerPrivilege.DIRECT)["flags"][2]
    assert "MARKET_OWNERSHIP_ENABLED" in pattern
    assert "DHAN" not in pattern and "PASSWORD" not in pattern and "TOKEN" not in pattern


# --- config sanitization ----------------------------------------------------------------------


def test_parse_and_sanitize_flags_on_off_missing_unknown() -> None:
    raw = "\n".join(
        [
            "MARKET_INGESTION_SERVICE_ENABLED=false",
            "MARKET_OWNERSHIP_ENABLED=true",
            "IGNORED_SECRET=shhh",  # not allow-listed -> dropped
            'MARKET_OWNERSHIP_LEASE_TTL_SECONDS="30"',
            "MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS=notint",
        ]
    )
    config = parse_config_lines(raw)
    assert "IGNORED_SECRET" not in config
    report = project_audit(_snapshot(flag_lines=raw))
    assert report.flags["MARKET_INGESTION_SERVICE_ENABLED"] is Flag.OFF
    assert report.flags["MARKET_OWNERSHIP_ENABLED"] is Flag.ON
    assert report.flags["IPC_AUTHORITATIVE_ENABLED"] is Flag.MISSING  # absent key
    assert report.ownership_ttl == "30"
    assert report.ownership_renewal == "UNKNOWN"  # non-numeric


def test_conflicting_duplicate_key_is_unknown_not_a_guess() -> None:
    raw = "MARKET_OWNERSHIP_ENABLED=true\nMARKET_OWNERSHIP_ENABLED=false"
    report = project_audit(_snapshot(flag_lines=raw))
    assert report.flags["MARKET_OWNERSHIP_ENABLED"] is Flag.UNKNOWN


def test_unrecognized_flag_value_is_unknown_never_on() -> None:
    report = project_audit(_snapshot(flag_lines="MARKET_OWNERSHIP_ENABLED=maybe"))
    assert report.flags["MARKET_OWNERSHIP_ENABLED"] is Flag.UNKNOWN
    assert report.safety_stop is False


# --- safety stop ------------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["MARKET_OWNERSHIP_ENABLED=true", "IPC_AUTHORITATIVE_ENABLED=on"])
def test_safety_stop_when_authority_flag_on(line: str) -> None:
    assert project_audit(_snapshot(flag_lines=line)).safety_stop is True


def test_no_safety_stop_when_all_off() -> None:
    raw = "MARKET_OWNERSHIP_ENABLED=false\nIPC_AUTHORITATIVE_ENABLED=0"
    assert project_audit(_snapshot(flag_lines=raw)).safety_stop is False


# --- revision comparison ----------------------------------------------------------------------


def test_revision_comparison_main_deployed() -> None:
    report = project_audit(_snapshot(build_sha=_MAIN_FULL))
    assert report.main_deployed is Tri.YES
    assert report.decoupling_deployed is Tri.NO


def test_revision_comparison_decoupling_deployed() -> None:
    report = project_audit(_snapshot(build_sha=_DECOUPLING_FULL))
    assert report.decoupling_deployed is Tri.YES
    assert report.main_deployed is Tri.NO


def test_unknown_build_sha_is_cannot_prove() -> None:
    report = project_audit(_snapshot(build_sha=None, version_responded=False))
    assert report.main_deployed is Tri.CANNOT_PROVE
    assert report.decoupling_deployed is Tri.CANNOT_PROVE
    assert report.backend_build_sha == "UNKNOWN"


def test_image_parity_yes_no_cannot_prove() -> None:
    assert project_audit(_snapshot()).backend_ingestion_revision_parity is Tri.YES
    diff = project_audit(_snapshot(ingestion_image=_OTHER_IMAGE))
    assert diff.backend_ingestion_revision_parity is Tri.NO
    missing = project_audit(_snapshot(ingestion_exists=False))
    assert missing.backend_ingestion_revision_parity is Tri.CANNOT_PROVE
    assert missing.ingestion_exists is Tri.NO


# --- secret safety of the rendered block ------------------------------------------------------


def test_rendered_block_leaks_no_secret() -> None:
    raw = "MARKET_OWNERSHIP_ENABLED=true\nMARKET_OWNERSHIP_LEASE_TTL_SECONDS=30"
    text = render(project_audit(_snapshot(flag_lines=raw))).lower()
    for secret in ("secret", "token", "password", "client_id", "redis://", "postgres://"):
        assert secret not in text
    assert "production_read_only_audit" in text
    assert "safety_stop=true" in text


# --- collect + fail-closed transport ----------------------------------------------------------


def test_collect_runs_only_allow_listed_commands_and_probes_version() -> None:
    executor = FakeExecutor(_container_handler())
    snap = collect(executor, _version(True, _MAIN_FULL), privilege=DockerPrivilege.DIRECT)
    assert snap.backend.running is True
    assert snap.backend.image == _IMAGE
    assert snap.backend_build_sha == _MAIN_FULL
    # every issued command is one of the fixed inspection-only commands (plus preflight `true`).
    allow = [["true"], *read_only_commands(DockerPrivilege.DIRECT).values()]
    for call in executor.calls:
        assert call in allow, f"unexpected command {call}"


def test_collect_marks_absent_container_when_inspect_fails() -> None:
    def handler(cmd: list[str]) -> ExecResult:
        if "apexscan-market-ingestion" in cmd:
            return ExecResult(1, "", "No such object")
        if cmd == ["true"]:
            return ExecResult(0, "", "")
        return ExecResult(0, "true\t" + _IMAGE + "\t", "")

    snap = collect(FakeExecutor(handler), _version(True, _MAIN_FULL))
    assert snap.ingestion.exists is False


def test_collect_fails_closed_on_ssh_host_verification() -> None:
    executor = FakeExecutor(lambda cmd: ExecResult(255, "", "Host key verification failed."))
    with pytest.raises(AuditError, match="SSH_HOST_VERIFICATION_FAILED"):
        collect(executor, _version(True, None))


def test_collect_fails_closed_when_sudo_unavailable() -> None:
    def handler(cmd: list[str]) -> ExecResult:
        if cmd == ["sudo", "-n", "true"]:
            return ExecResult(1, "", "sudo: a password is required")
        return ExecResult(0, "", "")

    with pytest.raises(AuditError, match="DOCKER_PRIVILEGE_UNAVAILABLE"):
        collect(
            handler_executor(handler),
            _version(True, None),
            privilege=DockerPrivilege.SUDO_NON_INTERACTIVE,
        )


# --- helpers ----------------------------------------------------------------------------------


def handler_executor(handler: Callable[[list[str]], ExecResult]) -> FakeExecutor:
    return FakeExecutor(handler)


def _container_handler() -> Callable[[list[str]], ExecResult]:
    def handler(cmd: list[str]) -> ExecResult:
        if cmd == ["true"]:
            return ExecResult(0, "", "")
        if "grep" in cmd:
            return ExecResult(0, "MARKET_OWNERSHIP_ENABLED=false", "")
        return ExecResult(0, "true\t" + _IMAGE + "\thealthy", "")

    return handler


def _snapshot(
    *,
    flag_lines: str = "",
    build_sha: str | None = _MAIN_FULL,
    version_responded: bool = True,
    ingestion_image: str = _IMAGE,
    ingestion_exists: bool = True,
):  # noqa: ANN202 - test builder
    from deploy.read_only_audit import ContainerRaw, RawSnapshot

    return RawSnapshot(
        backend=ContainerRaw(exists=True, running=True, image=_IMAGE, health="healthy"),
        ingestion=ContainerRaw(
            exists=ingestion_exists,
            running=ingestion_exists,
            image=ingestion_image if ingestion_exists else None,
        ),
        redis=ContainerRaw(exists=True, running=True, health="healthy"),
        postgres=ContainerRaw(exists=True, running=True, health="healthy"),
        flag_lines=flag_lines,
        version_responded=version_responded,
        backend_build_sha=build_sha,
    )
