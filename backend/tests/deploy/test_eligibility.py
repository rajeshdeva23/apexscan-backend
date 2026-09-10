"""SHA-eligibility gate for production promotion (DEPLOY-1)."""

from __future__ import annotations

import pytest

from deploy.eligibility import evaluate_eligibility

_MAIN = "a" * 40
_OTHER = "b" * 40


def test_valid_main_sha_with_green_ci_is_eligible() -> None:
    result = evaluate_eligibility(
        target_sha=_MAIN, main_ancestry=[_MAIN, _OTHER], ci_conclusions=["success", "success"]
    )
    assert result.eligible
    assert result.reasons == ()


def test_short_or_nonhex_sha_rejected() -> None:
    for bad in ("abc123", "deadbeef", "g" * 40, _MAIN[:39]):
        result = evaluate_eligibility(
            target_sha=bad, main_ancestry=[bad], ci_conclusions=["success"]
        )
        assert not result.eligible
        assert any("40-character" in r for r in result.reasons)


def test_sha_not_reachable_from_main_rejected() -> None:
    result = evaluate_eligibility(
        target_sha=_OTHER, main_ancestry=[_MAIN], ci_conclusions=["success"]
    )
    assert not result.eligible
    assert any("not reachable from origin/main" in r for r in result.reasons)


def test_missing_ci_runs_rejected() -> None:
    result = evaluate_eligibility(target_sha=_MAIN, main_ancestry=[_MAIN], ci_conclusions=[])
    assert not result.eligible
    assert any("no CI check runs" in r for r in result.reasons)


def test_non_success_ci_rejected() -> None:
    result = evaluate_eligibility(
        target_sha=_MAIN, main_ancestry=[_MAIN], ci_conclusions=["success", "failure"]
    )
    assert not result.eligible
    assert any("not green" in r for r in result.reasons)


def test_case_and_whitespace_insensitive() -> None:
    result = evaluate_eligibility(
        target_sha=f"  {_MAIN.upper()}  ",
        main_ancestry=[f"{_MAIN}\n"],
        ci_conclusions=[" SUCCESS "],
    )
    assert result.eligible


@pytest.mark.parametrize("arbitrary", ["c" * 40, "d" * 40])
def test_arbitrary_unapproved_branch_sha_never_eligible(arbitrary: str) -> None:
    # Not in main ancestry -> rejected even with green CI (unreviewed branch guard).
    result = evaluate_eligibility(
        target_sha=arbitrary, main_ancestry=[_MAIN], ci_conclusions=["success"]
    )
    assert not result.eligible
