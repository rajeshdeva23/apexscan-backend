"""SHA-eligibility gate for production promotion (DEPLOY-1; DEPLOY-ELIGIBILITY-FIX-1).

Covers the self-poisoning fix: eligibility is decided only by the required
source-validation checks (allow-list), using the latest run per check, and never by
the production deploy workflow's own job results.
"""

from __future__ import annotations

import subprocess

import pytest

import deploy.eligibility as eligibility
from deploy.eligibility import (
    REQUIRED_SOURCE_CHECKS,
    CheckRun,
    evaluate_eligibility,
)

_MAIN = "a" * 40
_OTHER = "b" * 40

_REQUIRED = tuple(sorted(REQUIRED_SOURCE_CHECKS))


def _run(
    name: str,
    *,
    conclusion: str | None = "success",
    status: str = "completed",
    started_at: str = "2026-09-11T06:00:00Z",
    run_id: int = 1,
    app_slug: str = "github-actions",
) -> CheckRun:
    return CheckRun(
        name=name,
        status=status,
        conclusion=conclusion,
        started_at=started_at,
        run_id=run_id,
        app_slug=app_slug,
    )


def _all_green() -> list[CheckRun]:
    """One successful Actions run for every required source check."""
    return [_run(name, run_id=i + 1) for i, name in enumerate(_REQUIRED)]


def _deploy_failures() -> list[CheckRun]:
    """Failed/skipped deploy-production.yml jobs on the same SHA (must be ignored)."""
    return [
        _run("Verify promotion eligibility", conclusion="failure", run_id=90),
        _run("Promote to production", conclusion="failure", run_id=91),
        _run("Promote to production", conclusion=None, status="completed", run_id=92),
    ]


def test_all_required_source_checks_green_is_eligible() -> None:
    result = evaluate_eligibility(
        target_sha=_MAIN, main_ancestry=[_MAIN, _OTHER], check_runs=_all_green()
    )
    assert result.eligible
    assert result.reasons == ()


def test_short_or_nonhex_sha_rejected() -> None:
    for bad in ("abc123", "deadbeef", "g" * 40, _MAIN[:39]):
        result = evaluate_eligibility(target_sha=bad, main_ancestry=[bad], check_runs=_all_green())
        assert not result.eligible
        assert any("40-character" in r for r in result.reasons)


def test_sha_not_reachable_from_main_rejected() -> None:
    result = evaluate_eligibility(target_sha=_OTHER, main_ancestry=[_MAIN], check_runs=_all_green())
    assert not result.eligible
    assert any("not reachable from origin/main" in r for r in result.reasons)


# --- §10 A/B/C/D: required-check presence and terminal state ---------------- #
def test_required_check_failed_is_ineligible() -> None:  # B
    runs = _all_green()
    runs[0] = _run(_REQUIRED[0], conclusion="failure")
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any("not green" in r and _REQUIRED[0] in r for r in result.reasons)


def test_required_check_missing_is_ineligible() -> None:  # C
    runs = [r for r in _all_green() if r.name != _REQUIRED[1]]
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any("not found" in r and _REQUIRED[1] in r for r in result.reasons)


def test_required_check_in_progress_is_ineligible() -> None:  # D
    runs = _all_green()
    runs[2] = _run(_REQUIRED[2], status="in_progress", conclusion=None)
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any("not completed" in r and _REQUIRED[2] in r for r in result.reasons)


def test_no_check_runs_is_ineligible() -> None:  # O
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=[])
    assert not result.eligible
    assert len(result.reasons) == len(_REQUIRED)


# --- §10 E/F/G/H: deploy-production outcomes never poison source eligibility - #
def test_green_source_with_failed_deploy_check_is_eligible() -> None:  # E
    result = evaluate_eligibility(
        target_sha=_MAIN,
        main_ancestry=[_MAIN],
        check_runs=_all_green() + _deploy_failures(),
    )
    assert result.eligible, result.reasons


def test_green_source_with_many_failed_deploys_is_eligible() -> None:  # F/G/H
    extra = _deploy_failures() * 3 + [
        _run("Verify promotion eligibility", conclusion=None, status="completed", run_id=200),
    ]
    result = evaluate_eligibility(
        target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=_all_green() + extra
    )
    assert result.eligible, result.reasons


# --- §10 I/J: latest-run (rerun) semantics --------------------------------- #
def test_failed_then_reran_success_is_eligible() -> None:  # I
    runs = _all_green()
    # An older failed run of a required check, superseded by a newer success.
    runs.append(
        _run(_REQUIRED[0], conclusion="failure", started_at="2026-09-11T05:00:00Z", run_id=10)
    )
    runs[0] = _run(_REQUIRED[0], conclusion="success", started_at="2026-09-11T07:00:00Z", run_id=11)
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert result.eligible, result.reasons


def test_success_then_reran_failure_is_ineligible() -> None:  # J
    runs = _all_green()
    runs[0] = _run(_REQUIRED[0], conclusion="success", started_at="2026-09-11T05:00:00Z", run_id=10)
    runs.append(
        _run(_REQUIRED[0], conclusion="failure", started_at="2026-09-11T07:00:00Z", run_id=11)
    )
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any("not green" in r and _REQUIRED[0] in r for r in result.reasons)


def test_same_started_at_uses_run_id_tiebreak() -> None:
    ts = "2026-09-11T06:00:00Z"
    runs = _all_green()
    runs[0] = _run(_REQUIRED[0], conclusion="failure", started_at=ts, run_id=5)
    runs.append(_run(_REQUIRED[0], conclusion="success", started_at=ts, run_id=6))  # newer id
    assert evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs).eligible


def test_newer_failure_with_empty_started_at_still_overrides_success() -> None:
    # A completed failure with a missing started_at must NOT sort below an older success:
    # run_id (always present, monotonic) is authoritative, so this stays fail-closed.
    runs = _all_green()
    runs[0] = _run(_REQUIRED[0], conclusion="success", started_at="2026-09-11T05:00:00Z", run_id=10)
    runs.append(_run(_REQUIRED[0], conclusion="failure", started_at="", run_id=11))
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any("not green" in r and _REQUIRED[0] in r for r in result.reasons)


@pytest.mark.parametrize("terminal", ["skipped", "cancelled", "neutral", "timed_out", None])
def test_required_check_non_success_conclusions_are_ineligible(terminal: str | None) -> None:
    runs = _all_green()
    runs[0] = _run(_REQUIRED[0], conclusion=terminal)
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any(_REQUIRED[0] in r for r in result.reasons)


# --- §10 K/L: spoofing / foreign checks ------------------------------------ #
def test_foreign_app_cannot_satisfy_required_check() -> None:  # K
    runs = [r for r in _all_green() if r.name != _REQUIRED[0]]
    # A same-named check from a different app must NOT satisfy the required check.
    runs.append(_run(_REQUIRED[0], conclusion="success", app_slug="totally-not-actions"))
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs)
    assert not result.eligible
    assert any("not found" in r and _REQUIRED[0] in r for r in result.reasons)


def test_unknown_checks_are_ignored_not_required() -> None:  # L
    runs = _all_green() + [_run("Some New Workflow", conclusion="failure", run_id=77)]
    assert evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], check_runs=runs).eligible


def test_case_and_whitespace_insensitive_sha_and_conclusion() -> None:
    runs = [_run(name, conclusion=" SUCCESS ") for name in _REQUIRED]
    result = evaluate_eligibility(
        target_sha=f"  {_MAIN.upper()}  ", main_ancestry=[f"{_MAIN}\n"], check_runs=runs
    )
    assert result.eligible


@pytest.mark.parametrize("arbitrary", ["c" * 40, "d" * 40])
def test_arbitrary_unapproved_branch_sha_never_eligible(arbitrary: str) -> None:
    result = evaluate_eligibility(
        target_sha=arbitrary, main_ancestry=[_MAIN], check_runs=_all_green()
    )
    assert not result.eligible


def test_deploy_jobs_are_not_in_required_set() -> None:
    assert "Promote to production" not in REQUIRED_SOURCE_CHECKS
    assert "Verify promotion eligibility" not in REQUIRED_SOURCE_CHECKS
    assert REQUIRED_SOURCE_CHECKS == {
        "Backend quality and security",
        "Compose runtime validation",
        "Quality gates, build, publish",
    }


# --- §10 M/N: I/O layer fails closed --------------------------------------- #
def test_check_runs_api_error_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:  # N
    def _boom(_args: list[str]) -> str:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(eligibility, "_run", _boom)
    assert eligibility._check_runs("owner/repo", _MAIN) == []


def test_check_runs_skips_malformed_lines(monkeypatch: pytest.MonkeyPatch) -> None:  # M
    good = (
        '{"name":"Backend quality and security","status":"completed",'
        '"conclusion":"success","started_at":"2026-09-11T06:00:00Z","id":1,'
        '"app_slug":"github-actions"}'
    )
    monkeypatch.setattr(eligibility, "_run", lambda _args: good + "\nnot-json\n{bad}\n")
    runs = eligibility._check_runs("owner/repo", _MAIN)
    assert len(runs) == 1
    assert runs[0].name == "Backend quality and security"
    assert runs[0].app_slug == "github-actions"


def test_check_runs_missing_name_key_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eligibility, "_run", lambda _args: '{"status":"completed"}')
    assert eligibility._check_runs("owner/repo", _MAIN) == []
