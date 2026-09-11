"""Production-promotion SHA eligibility (DEPLOY-1; DEPLOY-ELIGIBILITY-FIX-1).

Pure decision logic the manual production deploy workflow uses to reject any
target that is not a full-length commit reachable from ``origin/main`` whose
required **source-validation** checks are all green.

Eligibility answers only "did the required source/build validation for this exact
SHA pass?" — never "has every workflow ever run against this SHA succeeded?". The
production deploy workflow (``deploy-production.yml``) posts its own job results as
check-runs on the same source commit; those are deployment *outcomes*, not source
eligibility, so they are excluded via an explicit allow-list of required checks
(exclusion lists fail open when new workflows appear — an allow-list fails closed).

For each required check the **latest** run for the SHA is authoritative, so a green
rerun clears an older failure and a failed rerun overrides an older success. A
required check that is missing, not completed, or not ``success`` — from any cause,
including a GitHub API failure — fails closed (ineligible).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# The GitHub App that must own a required check; a same-named check from any other
# app can never satisfy a required source check (spoofing guard).
_ACTIONS_APP_SLUG = "github-actions"

# Authoritative source-validation checks (GitHub Actions job names). These establish
# that the exact SHA is safe to promote: CI quality/security + compose runtime
# validation (ci.yml) and the SHA-pinned image build/publish (build-image.yml).
# deploy-production.yml jobs ("Verify promotion eligibility", "Promote to production")
# are deliberately absent — deployment results never gate source eligibility.
REQUIRED_SOURCE_CHECKS: frozenset[str] = frozenset(
    {
        "Backend quality and security",
        "Compose runtime validation",
        "Quality gates, build, publish",
    }
)


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One GitHub check-run for the target SHA (only the fields eligibility needs).

    Attributes:
        name: The check-run name (equal to the workflow job's display name).
        status: The run status (``queued`` / ``in_progress`` / ``completed``).
        conclusion: The terminal conclusion (``success`` / ``failure`` / ...), or
            None while not yet completed.
        started_at: ISO-8601 start time (informational only; may be empty when unknown).
        run_id: The check-run id; GitHub assigns it monotonically at creation, so it
            is the authoritative "which run is newest" ordering for reruns.
        app_slug: The owning GitHub App slug (``github-actions`` for Actions).
    """

    name: str
    status: str
    conclusion: str | None
    started_at: str
    run_id: int
    app_slug: str


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Outcome of evaluating a promotion target.

    Attributes:
        eligible: Whether the target may be promoted to production.
        reasons: Human-readable reasons it was rejected (empty when eligible).
    """

    eligible: bool
    reasons: tuple[str, ...]


def _latest_required_run(check_runs: Sequence[CheckRun], name: str) -> CheckRun | None:
    """Return the most recent Actions-owned run of the required check ``name``, or None.

    Only check-runs from the GitHub Actions app are considered, so a same-named check
    from an unrelated app cannot satisfy a required source check. "Most recent" is the
    greatest check-run ``id``: GitHub assigns ids monotonically at creation, so a rerun
    always has a higher id than the run it supersedes. The id is used (not ``started_at``)
    because it is always present — a run with a missing/empty ``started_at`` must never be
    able to sort *below* an older run and let a stale success mask a newer failure.
    """
    candidates = [
        run for run in check_runs if run.name == name and run.app_slug == _ACTIONS_APP_SLUG
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda run: run.run_id)


def evaluate_eligibility(
    *,
    target_sha: str,
    main_ancestry: Sequence[str],
    check_runs: Sequence[CheckRun],
    required_checks: frozenset[str] = REQUIRED_SOURCE_CHECKS,
) -> EligibilityResult:
    """Decide whether ``target_sha`` may be promoted to production.

    Args:
        target_sha: The requested commit to deploy.
        main_ancestry: Full SHAs reachable from ``origin/main`` (``git rev-list``).
        check_runs: All check-runs reported for ``target_sha``.
        required_checks: The source-validation checks that must each be green
            (defaults to :data:`REQUIRED_SOURCE_CHECKS`).

    Returns:
        An :class:`EligibilityResult`; ``eligible`` is true only when the SHA is a
        full commit hash, is contained in ``main_ancestry``, and every required
        source check's latest Actions run completed with conclusion ``success``.
    """
    reasons: list[str] = []
    sha = target_sha.strip().lower()
    if not _FULL_SHA.match(sha):
        reasons.append("target SHA must be a full 40-character commit hash")
    elif sha not in {candidate.strip().lower() for candidate in main_ancestry}:
        reasons.append("target SHA is not reachable from origin/main")

    if not required_checks:
        reasons.append("no required source checks are configured")
    for name in sorted(required_checks):
        latest = _latest_required_run(check_runs, name)
        if latest is None:
            reasons.append(f"required source check not found for target SHA: {name}")
        elif latest.status != "completed":
            reasons.append(f"required source check not completed ({latest.status}): {name}")
        elif (latest.conclusion or "").strip().lower() != "success":
            reasons.append(f"required source check not green ({latest.conclusion}): {name}")
    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))


def _run(args: list[str]) -> str:
    """Run a trusted command and return trimmed stdout, raising on failure."""
    completed = subprocess.run(args, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _main_ancestry() -> list[str]:
    """Full SHAs reachable from ``origin/main`` on the runner."""
    return _run(["git", "rev-list", "origin/main"]).splitlines()


def _check_runs(repo: str, sha: str) -> list[CheckRun]:
    """Fetch check-runs for ``sha`` via the GitHub CLI (paginated), parsed defensively.

    Any GitHub CLI failure, non-JSON body, or unexpected shape is treated as "no
    runs" so eligibility fails closed rather than crashing — an uncertain CI state
    must never promote to production. Individual malformed entries are skipped.
    """
    try:
        raw = _run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/commits/{sha}/check-runs",
                "--jq",
                (".check_runs[] | {name, status, conclusion, started_at, id, app_slug: .app.slug}"),
            ]
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    runs: list[CheckRun] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            runs.append(
                CheckRun(
                    name=str(item["name"]),
                    status=str(item.get("status") or ""),
                    conclusion=(item.get("conclusion")),
                    started_at=str(item.get("started_at") or ""),
                    run_id=int(item.get("id") or 0),
                    app_slug=str(item.get("app_slug") or ""),
                )
            )
        except (ValueError, TypeError, KeyError):
            continue  # skip a malformed entry; a missing required check fails closed
    return runs


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: verify eligibility of a target SHA; exit non-zero if ineligible."""
    parser = argparse.ArgumentParser(description="Verify production-promotion eligibility.")
    parser.add_argument("--sha", required=True, help="Target commit SHA to promote.")
    parser.add_argument("--repo", required=True, help="owner/name for the GitHub repository.")
    args = parser.parse_args(argv)

    result = evaluate_eligibility(
        target_sha=args.sha,
        main_ancestry=_main_ancestry(),
        check_runs=_check_runs(args.repo, args.sha),
    )
    if result.eligible:
        print(f"ELIGIBLE {args.sha}")
        return 0
    for reason in result.reasons:
        print(f"INELIGIBLE: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
