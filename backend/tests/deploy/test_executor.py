"""Hardened SSH executor: argv safety and host-key pinning (DEPLOY-2)."""

from __future__ import annotations

import subprocess

import pytest

from deploy.executor import ExecResult, SSHExecutor, TransportConfigError


def _executor() -> SSHExecutor:
    return SSHExecutor(
        host="host.example",
        user="deployer",
        key_file="/tmp/key",
        known_hosts_file="/tmp/known_hosts",
        connect_timeout=7,
    )


def _argv(command: list[str]) -> list[str]:
    return _executor()._ssh_argv(command)


def test_ssh_argv_is_host_key_pinned_and_batch() -> None:
    argv = _argv(["docker", "info"])
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined
    assert "StrictHostKeyChecking=yes" in joined
    assert "UserKnownHostsFile=/tmp/known_hosts" in joined
    assert "IdentitiesOnly=yes" in joined
    assert "ConnectTimeout=7" in joined
    assert "-i" in argv and "/tmp/key" in argv
    assert "deployer@host.example" in argv


def test_ssh_never_disables_host_key_checking() -> None:
    assert "StrictHostKeyChecking=no" not in " ".join(_argv(["true"]))


def test_remote_command_is_shlex_quoted() -> None:
    # The remote command is the final argv element, safely quoted (no injection).
    argv = _argv(["echo", "a; rm -rf /"])
    assert argv[-1] == "echo 'a; rm -rf /'"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "bad host"},
        {"host": "h;rm"},
        {"user": "bad user"},
        {"user": "a$b"},
    ],
)
def test_invalid_host_or_user_rejected(kwargs: dict[str, str]) -> None:
    base = {
        "host": "h",
        "user": "u",
        "key_file": "/k",
        "known_hosts_file": "/kh",
    }
    with pytest.raises(TransportConfigError):
        SSHExecutor(**{**base, **kwargs})


@pytest.mark.parametrize("missing", ["key_file", "known_hosts_file"])
def test_missing_key_material_rejected(missing: str) -> None:
    base = {"host": "h", "user": "u", "key_file": "/k", "known_hosts_file": "/kh"}
    with pytest.raises(TransportConfigError):
        SSHExecutor(**{**base, missing: ""})


def test_run_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _executor().run(["docker", "info"])
    assert result == ExecResult(0, "out", "err")
    assert result.ok


def test_run_timeout_returns_124(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _executor().run(["docker", "info"], timeout=5)
    assert result.returncode == 124
    assert not result.ok
