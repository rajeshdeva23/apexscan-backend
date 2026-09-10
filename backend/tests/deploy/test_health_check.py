"""Bounded post-deploy release verification (DEPLOY-1)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from deploy.health_check import ProbeResult, VerifyOutcome, verify_release

_SHA = "a" * 40


def _sequence(results: Sequence[ProbeResult]) -> object:
    it = iter(results)
    last = results[-1]

    def probe() -> ProbeResult:
        return next(it, last)

    return probe


def test_success_when_healthy_and_sha_matches() -> None:
    probe = _sequence([ProbeResult(True, True, True, _SHA)])
    result = verify_release(probe, expected_sha=_SHA, max_attempts=5)
    assert result.outcome is VerifyOutcome.SUCCESS
    assert result.attempts == 1


def test_wrong_sha_when_healthy_but_mismatch() -> None:
    probe = _sequence([ProbeResult(True, True, True, "b" * 40)])
    result = verify_release(probe, expected_sha=_SHA, max_attempts=5)
    assert result.outcome is VerifyOutcome.WRONG_SHA
    assert result.observed_sha == "b" * 40


def test_becomes_ready_after_retries() -> None:
    probe = _sequence(
        [
            ProbeResult(True, False, False, None),
            ProbeResult(True, True, False, None),
            ProbeResult(True, True, True, _SHA),
        ]
    )
    result = verify_release(probe, expected_sha=_SHA, max_attempts=5)
    assert result.outcome is VerifyOutcome.SUCCESS
    assert result.attempts == 3


def test_health_failure_exhausted() -> None:
    probe = _sequence([ProbeResult(True, False, False, None)])
    result = verify_release(probe, expected_sha=_SHA, max_attempts=3)
    assert result.outcome is VerifyOutcome.HEALTH_FAILED
    assert result.attempts == 3


def test_readiness_failure_exhausted() -> None:
    probe = _sequence([ProbeResult(True, True, False, None)])
    result = verify_release(probe, expected_sha=_SHA, max_attempts=2)
    assert result.outcome is VerifyOutcome.READINESS_FAILED


def test_timeout_when_never_starts() -> None:
    probe = _sequence([ProbeResult(False, False, False, None)])
    result = verify_release(probe, expected_sha=_SHA, max_attempts=4)
    assert result.outcome is VerifyOutcome.TIMEOUT
    assert result.attempts == 4


def test_sleep_called_between_attempts_only() -> None:
    calls = {"n": 0}

    def sleep() -> None:
        calls["n"] += 1

    probe = _sequence([ProbeResult(True, False, False, None)])
    verify_release(probe, expected_sha=_SHA, max_attempts=3, sleep=sleep)
    assert calls["n"] == 2  # slept between the 3 attempts, not after the last


def test_zero_attempts_rejected() -> None:
    probe = _sequence([ProbeResult(True, True, True, _SHA)])
    with pytest.raises(ValueError, match="max_attempts"):
        verify_release(probe, expected_sha=_SHA, max_attempts=0)
