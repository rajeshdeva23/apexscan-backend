"""Bounded post-deploy release verification (DEPLOY-1).

Polls the deployed application's existing startup/health/readiness/version
endpoints a bounded number of times and confirms the running build SHA matches
the promoted commit. The core is a pure state machine with I/O injected, so
every outcome — success, wrong SHA, startup/health/readiness failure, and never
coming up (timeout) — is unit tested. It never blocks forever.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class VerifyOutcome(StrEnum):
    """Terminal result of verifying a deployed release."""

    SUCCESS = "success"
    WRONG_SHA = "wrong_sha"
    STARTUP_FAILED = "startup_failed"
    HEALTH_FAILED = "health_failed"
    READINESS_FAILED = "readiness_failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One observation of the deployed app's probes.

    Attributes:
        startup_ok: Whether ``GET /health/startup`` reports started.
        health_ok: Whether ``GET /health`` reports live.
        ready_ok: Whether ``GET /health/ready`` reports ready.
        version_sha: ``build_sha`` from ``GET /version``, or None if unreachable.
    """

    startup_ok: bool
    health_ok: bool
    ready_ok: bool
    version_sha: str | None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of a bounded verification run."""

    outcome: VerifyOutcome
    attempts: int
    observed_sha: str | None


def _stage_failure(probe: ProbeResult) -> VerifyOutcome | None:
    """Return the first failing stage for an attempt, or None if all stages pass."""
    if not probe.startup_ok:
        return VerifyOutcome.STARTUP_FAILED
    if not probe.health_ok:
        return VerifyOutcome.HEALTH_FAILED
    if not probe.ready_ok:
        return VerifyOutcome.READINESS_FAILED
    return None


def verify_release(
    probe: Callable[[], ProbeResult],
    *,
    expected_sha: str,
    max_attempts: int,
    sleep: Callable[[], None] = lambda: None,
) -> VerifyResult:
    """Verify a deployed release within a bounded number of attempts.

    On each attempt the probes are read once. When startup, health, and
    readiness all pass, the running ``build_sha`` is authoritative: it either
    matches ``expected_sha`` (SUCCESS) or does not (WRONG_SHA) and returns
    immediately. Otherwise the attempt is retried after ``sleep`` until
    ``max_attempts`` is reached, at which point the last failing stage is
    returned — or TIMEOUT if the app never even started.

    Args:
        probe: Reads the current probe observation (injected I/O).
        expected_sha: The promoted commit the running app must report.
        max_attempts: Maximum probe attempts (must be >= 1).
        sleep: Called between attempts; defaults to a no-op for tests.

    Returns:
        The terminal :class:`VerifyResult`.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    expected = expected_sha.strip().lower()
    last = ProbeResult(False, False, False, None)
    ever_started = False
    for attempt in range(1, max_attempts + 1):
        last = probe()
        ever_started = ever_started or last.startup_ok
        stage_failure = _stage_failure(last)
        if stage_failure is None:
            matched = (last.version_sha or "").strip().lower() == expected
            outcome = VerifyOutcome.SUCCESS if matched else VerifyOutcome.WRONG_SHA
            return VerifyResult(outcome, attempt, last.version_sha)
        if attempt < max_attempts:
            sleep()
    outcome = _stage_failure(last) or VerifyOutcome.TIMEOUT
    if not ever_started:
        outcome = VerifyOutcome.TIMEOUT
    return VerifyResult(outcome, max_attempts, last.version_sha)


def _get_json(url: str, timeout: float) -> dict[str, object] | None:
    """GET a URL and parse JSON, or return None on any transport/parse failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _http_probe(base_url: str, timeout: float) -> ProbeResult:
    """Read the real probes from a running app (used by the CLI)."""
    startup = _get_json(f"{base_url}/api/v1/health/startup", timeout)
    health = _get_json(f"{base_url}/api/v1/health", timeout)
    ready = _get_json(f"{base_url}/api/v1/health/ready", timeout)
    version = _get_json(f"{base_url}/api/v1/version", timeout)
    version_sha = str(version["build_sha"]) if version and "build_sha" in version else None
    return ProbeResult(
        startup_ok=startup is not None,
        health_ok=health is not None,
        ready_ok=ready is not None,
        version_sha=version_sha,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: verify a deployed release; exit non-zero unless the outcome is SUCCESS."""
    parser = argparse.ArgumentParser(description="Verify a deployed ApexScan release.")
    parser.add_argument("--base-url", required=True, help="Base URL of the deployed app.")
    parser.add_argument("--expected-sha", required=True, help="Promoted commit SHA.")
    parser.add_argument("--attempts", type=int, default=30, help="Max probe attempts.")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between attempts.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout seconds.")
    args = parser.parse_args(argv)

    result = verify_release(
        lambda: _http_probe(args.base_url.rstrip("/"), args.timeout),
        expected_sha=args.expected_sha,
        max_attempts=args.attempts,
        sleep=lambda: time.sleep(args.interval),
    )
    stream = sys.stdout if result.outcome is VerifyOutcome.SUCCESS else sys.stderr
    print(
        f"{result.outcome.value} attempts={result.attempts} observed_sha={result.observed_sha}",
        file=stream,
    )
    return 0 if result.outcome is VerifyOutcome.SUCCESS else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
