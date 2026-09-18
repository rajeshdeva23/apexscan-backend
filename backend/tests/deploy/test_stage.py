"""Offline tests for production release staging (DEPLOY-3C).

Prove the stager: verifies the bundle BEFORE any host command; installs ONLY
docker-compose.production.yml at the exact ``<deploy_root>/<sha>/``; is atomic and
idempotent (identical release = no-op success, differing release = fail closed);
never pulls/ups/restarts/prunes, never touches env/secrets/Dhan/authority, never
modifies a sibling release; fails closed on a bad bundle, path, SSH, or privilege; and
produces exactly the release path the transport preflight expects.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from deploy.bundle import PRODUCTION_COMPOSE_FILE, create_bundle
from deploy.executor import ExecResult, SSHExecutor
from deploy.stage import StageConfig, StageOutcome, stage
from deploy.transport import DockerPrivilege, resolve_release_path

_SHA = "a" * 40
_ROOT = "/opt/apexscan/releases"
_COMPOSE = b"services:\n  backend:\n    image: ${APEXSCAN_IMAGE}\n"
_RELEASE_DIR = f"{_ROOT}/{_SHA}"
_RELEASE_FILE = f"{_RELEASE_DIR}/{PRODUCTION_COMPOSE_FILE}"
_HASH = hashlib.sha256(_COMPOSE).hexdigest()

# Mutating / forbidden tokens that must never appear in any staging host command.
_FORBIDDEN = {
    "docker",
    "compose",
    "pull",
    "up",
    "down",
    "restart",
    "stop",
    "start",
    "rm",
    "rmi",
    "prune",
    "systemctl",
    "env",
    "printenv",
    "curl",
    "wget",
    "redis-cli",
    "psql",
    "--build",
    "MARKET_OWNERSHIP_ENABLED",
    "IPC_AUTHORITATIVE_ENABLED",
    "DHAN_RAW_LTT_DIAGNOSTIC_ENABLED",
}


def _bundle(sha: str = _SHA, content: bytes = _COMPOSE) -> bytes:
    return create_bundle(sha, {PRODUCTION_COMPOSE_FILE: content})


def _cfg(deploy_root: str = _ROOT, sha: str = _SHA) -> StageConfig:
    return StageConfig(
        deploy_root=deploy_root,
        target_sha=sha,
        docker_privilege=DockerPrivilege.SUDO_NON_INTERACTIVE,
    )


def _sudo(*args: str) -> list[str]:
    return ["sudo", "-n", *args]


class FakeExecutor:
    """Records every issued command and returns programmed results."""

    def __init__(self, handler: Callable[[list[str]], ExecResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._handler = handler or (lambda _cmd: ExecResult(0, "", ""))

    def run(self, command, *, timeout=None) -> ExecResult:  # noqa: ANN001
        cmd = list(command)
        self.calls.append(cmd)
        return self._handler(cmd)


class HostSim:
    """Stateful host: file absent by default; a write flips subsequent reads to written_hash."""

    def __init__(
        self,
        *,
        reachable: bool = True,
        sudo: bool = True,
        existing: str | None = None,
        written_hash: str = _HASH,
        mkdir_ok: bool = True,
        write_ok: bool = True,
    ) -> None:
        self.reachable = reachable
        self.sudo = sudo
        self.existing = existing
        self.written_hash = written_hash
        self.mkdir_ok = mkdir_ok
        self.write_ok = write_ok
        self.written = False

    def __call__(self, cmd: list[str]) -> ExecResult:
        if cmd == ["true"]:
            return ExecResult(0 if self.reachable else 255, "", "")
        if cmd == ["sudo", "-n", "true"]:
            return ExecResult(0 if self.sudo else 1, "", "")
        core = cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd
        verb = core[0]
        if verb == "test":
            return ExecResult(0 if self.existing is not None else 1, "", "")
        if verb == "mkdir":
            return ExecResult(0 if self.mkdir_ok else 1, "", "")
        if verb == "sh":
            self.written = True
            return ExecResult(0 if self.write_ok else 1, "", "")
        if verb == "sha256sum":
            value = self.written_hash if self.written else self.existing
            return ExecResult(0, f"{value}  {core[-1]}\n", "") if value else ExecResult(1, "", "")
        return ExecResult(0, "", "")


# --- happy path: staged, sanitized, only the compose file --------------------------------------


def test_stage_installs_only_compose_at_exact_path() -> None:
    ex = FakeExecutor(HostSim())
    res = stage(ex, _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.STAGED
    assert res.changed is True
    assert res.release_path == _RELEASE_FILE
    assert _sudo("mkdir", "-p", _RELEASE_DIR) in ex.calls
    write = next(c for c in ex.calls if c[2:3] == ["sh"])
    payload = write[-2]  # positional $2 to the write script
    assert base64.b64decode(payload) == _COMPOSE  # exactly the compose bytes, nothing else
    assert write[-1] == PRODUCTION_COMPOSE_FILE  # positional $3 is the only filename


def test_staged_path_equals_transport_preflight_expectation() -> None:
    res = stage(FakeExecutor(HostSim()), _cfg(), bundle=_bundle())
    assert res.release_path == f"{resolve_release_path(_ROOT, _SHA)}/{PRODUCTION_COMPOSE_FILE}"


def test_no_host_command_is_mutating_or_touches_env_dhan_authority() -> None:
    ex = FakeExecutor(HostSim())
    stage(ex, _cfg(), bundle=_bundle())
    for cmd in ex.calls:
        assert not (_FORBIDDEN & set(cmd)), f"forbidden token in {cmd}"


def test_sanitized_log_has_no_payload_or_secret(caplog) -> None:
    with caplog.at_level(logging.INFO):
        stage(FakeExecutor(HostSim()), _cfg(), bundle=_bundle())
    text = caplog.text
    assert "RELEASE_STAGING" in text
    assert base64.b64encode(_COMPOSE).decode() not in text
    assert "APEXSCAN_IMAGE" not in text  # no compose contents in the log line


# --- verify-before-mutation: bad bundles never touch the host ----------------------------------


def test_wrong_sha_bundle_rejected_before_any_host_call() -> None:
    ex = FakeExecutor(HostSim())
    res = stage(ex, _cfg(sha=_SHA), bundle=_bundle(sha="b" * 40))
    assert res.outcome is StageOutcome.BUNDLE_INVALID
    assert ex.calls == []  # verification ran before the executor was touched


def test_tampered_bundle_rejected_before_any_host_call() -> None:
    ex = FakeExecutor(HostSim())
    tampered = _tamper(_bundle(), PRODUCTION_COMPOSE_FILE, b"evil: true\n")
    res = stage(ex, _cfg(), bundle=tampered)
    assert res.outcome is StageOutcome.BUNDLE_INVALID
    assert ex.calls == []


@pytest.mark.parametrize("name", ["../evil.yml", "/etc/passwd"])
def test_path_traversal_bundle_rejected(name: str) -> None:
    ex = FakeExecutor(HostSim())
    res = stage(
        ex,
        _cfg(),
        bundle=_malicious_tar(name),
    )
    assert res.outcome is StageOutcome.BUNDLE_INVALID
    assert ex.calls == []


def test_symlink_bundle_rejected() -> None:
    ex = FakeExecutor(HostSim())
    res = stage(ex, _cfg(), bundle=_malicious_tar(PRODUCTION_COMPOSE_FILE, symlink=True))
    assert res.outcome is StageOutcome.BUNDLE_INVALID
    assert ex.calls == []


def test_bundle_with_extra_file_rejected_as_unexpected_content() -> None:
    ex = FakeExecutor(HostSim())
    extra = create_bundle(_SHA, {PRODUCTION_COMPOSE_FILE: _COMPOSE, "docker-compose.yml": b"x"})
    res = stage(ex, _cfg(), bundle=extra)
    assert res.outcome is StageOutcome.BUNDLE_UNEXPECTED_CONTENT
    assert ex.calls == []  # rejected before any host mutation


# --- path safety -------------------------------------------------------------------------------


@pytest.mark.parametrize("root", ["relative/releases", "/opt/../etc", "/opt/apex releases"])
def test_invalid_deploy_root_fails_closed_without_host_calls(root: str) -> None:
    ex = FakeExecutor(HostSim())
    res = stage(ex, _cfg(deploy_root=root), bundle=_bundle())
    assert res.outcome is StageOutcome.RELEASE_PATH_INVALID
    assert ex.calls == []


# --- idempotency & conflict --------------------------------------------------------------------


def test_identical_existing_release_is_idempotent_no_write() -> None:
    ex = FakeExecutor(HostSim(existing=_HASH))
    res = stage(ex, _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.ALREADY_STAGED
    assert res.changed is False
    assert not any(c[2:3] == ["mkdir"] or c[2:3] == ["sh"] for c in ex.calls)  # nothing written


def test_differing_existing_release_fails_closed_no_write() -> None:
    ex = FakeExecutor(HostSim(existing="deadbeef"))
    res = stage(ex, _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.RELEASE_CONFLICT
    assert res.changed is False
    assert not any(c[2:3] == ["mkdir"] or c[2:3] == ["sh"] for c in ex.calls)


# --- sibling release preservation --------------------------------------------------------------


def test_staging_never_touches_a_sibling_release() -> None:
    ex = FakeExecutor(HostSim())
    stage(ex, _cfg(), bundle=_bundle())
    other_sha = "c" * 40
    for cmd in ex.calls:
        assert other_sha not in " ".join(cmd)  # never names another release
        assert "mkdir" not in cmd or "-p" in cmd  # mkdir is non-destructive
        assert not ({"rm", "rmi", "prune", "-rf", "-r"} & set(cmd))  # no delete/prune


# --- fail-closed transport / privilege ---------------------------------------------------------


def test_ssh_unreachable_fails_closed() -> None:
    res = stage(FakeExecutor(HostSim(reachable=False)), _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.SSH_UNREACHABLE


def test_sudo_unavailable_fails_closed() -> None:
    res = stage(FakeExecutor(HostSim(sudo=False)), _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.PRIVILEGE_UNAVAILABLE


def test_mkdir_failure_fails_closed() -> None:
    res = stage(FakeExecutor(HostSim(mkdir_ok=False)), _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.MKDIR_FAILED


def test_write_failure_fails_closed() -> None:
    res = stage(FakeExecutor(HostSim(write_ok=False)), _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.WRITE_FAILED


def test_post_write_hash_mismatch_fails_closed() -> None:
    res = stage(FakeExecutor(HostSim(written_hash="0" * 64)), _cfg(), bundle=_bundle())
    assert res.outcome is StageOutcome.VERIFY_FAILED


# --- SSH hardening (the CLI's transport) -------------------------------------------------------


def test_ssh_executor_argv_is_hardened() -> None:
    argv = SSHExecutor(host="h", user="u", key_file="/k", known_hosts_file="/kh")._ssh_argv(
        ["true"]
    )
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined
    assert "StrictHostKeyChecking=yes" in joined
    assert "IdentitiesOnly=yes" in joined
    assert "StrictHostKeyChecking=no" not in joined


# --- the transport release-path preflight is left intact ---------------------------------------


def test_transport_release_path_preflight_unchanged() -> None:
    text = (Path(__file__).resolve().parents[2] / "deploy" / "transport.py").read_text("utf-8")
    assert "return TransportOutcome.REMOTE_PATH_INVALID" in text
    assert '"test", "-d", cfg.release_path' in text


# --- helpers -----------------------------------------------------------------------------------


def _tamper(archive: bytes, name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as src:
        members = {m.name: src.extractfile(m).read() for m in src.getmembers() if m.isreg()}  # type: ignore[union-attr]
    members[name] = data
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for n, d in members.items():
            info = tarfile.TarInfo(n)
            info.size = len(d)
            tar.addfile(info, io.BytesIO(d))
    return out.getvalue()


def _malicious_tar(name: str, *, symlink: bool = False) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        else:
            data = b"x"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()
