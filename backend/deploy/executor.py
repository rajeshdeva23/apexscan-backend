"""Remote command execution for the production transport (DEPLOY-2).

A narrow :class:`RemoteExecutor` seam so the deployment orchestration in
``deploy.transport`` is fully testable offline with a fake. The real
:class:`SSHExecutor` builds a hardened, injection-resistant ``ssh`` argv:
explicit host-key pinning (``StrictHostKeyChecking=yes`` against a controlled
``known_hosts``), ``BatchMode``/``IdentitiesOnly``, bounded ``ConnectTimeout``,
and a ``shlex``-quoted remote command. It never disables host verification and
never echoes secrets.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_HOST = re.compile(r"^[A-Za-z0-9._-]+$")
_USER = re.compile(r"^[A-Za-z0-9._-]+$")
_TIMEOUT_RC = 124


class TransportConfigError(ValueError):
    """Raised when SSH transport parameters are missing or malformed."""


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of one remote command.

    Attributes:
        returncode: Process exit status (``124`` on timeout).
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command exited zero."""
        return self.returncode == 0


@runtime_checkable
class RemoteExecutor(Protocol):
    """Runs a command on the deployment target and returns its result."""

    def run(self, command: Sequence[str], *, timeout: float | None = None) -> ExecResult:
        """Run ``command`` (argv list) remotely, capturing output; never raises on nonzero."""
        ...


class SSHExecutor:
    """Runs commands on the production host over a hardened, host-key-pinned SSH."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        key_file: str,
        known_hosts_file: str,
        connect_timeout: int = 10,
    ) -> None:
        if not _HOST.match(host):
            raise TransportConfigError("invalid SSH host")
        if not _USER.match(user):
            raise TransportConfigError("invalid SSH user")
        if not key_file or not known_hosts_file:
            raise TransportConfigError("SSH key_file and known_hosts_file are required")
        self._host = host
        self._user = user
        self._key_file = key_file
        self._known_hosts_file = known_hosts_file
        self._connect_timeout = connect_timeout

    def _ssh_argv(self, command: Sequence[str]) -> list[str]:
        """Build the hardened ssh argv for a remote command (host-key pinned)."""
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._known_hosts_file}",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"ConnectTimeout={self._connect_timeout}",
            "-i",
            self._key_file,
            f"{self._user}@{self._host}",
            shlex.join(command),
        ]

    def run(self, command: Sequence[str], *, timeout: float | None = None) -> ExecResult:
        """Run ``command`` on the host over SSH; return its result (never raises on nonzero)."""
        try:
            completed = subprocess.run(
                self._ssh_argv(command), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return ExecResult(_TIMEOUT_RC, "", "ssh command timed out")
        return ExecResult(completed.returncode, completed.stdout, completed.stderr)
