"""Production-promotion SHA eligibility (DEPLOY-1).

Pure decision logic the manual production deploy workflow uses to reject any
target that is not a full-length commit reachable from ``origin/main`` with a
green CI run. Fails closed: a target is eligible only when every check passes.
The ``__main__`` CLI gathers real inputs via ``git`` and ``gh`` and exits
non-zero on ineligibility so the workflow stops before touching production.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Outcome of evaluating a promotion target.

    Attributes:
        eligible: Whether the target may be promoted to production.
        reasons: Human-readable reasons it was rejected (empty when eligible).
    """

    eligible: bool
    reasons: tuple[str, ...]


def evaluate_eligibility(
    *,
    target_sha: str,
    main_ancestry: Sequence[str],
    ci_conclusions: Sequence[str],
) -> EligibilityResult:
    """Decide whether ``target_sha`` may be promoted to production.

    Args:
        target_sha: The requested commit to deploy.
        main_ancestry: Full SHAs reachable from ``origin/main`` (``git rev-list``).
        ci_conclusions: CI check-run conclusions reported for ``target_sha``.

    Returns:
        An :class:`EligibilityResult`; ``eligible`` is true only when the SHA is
        a full commit hash, is contained in ``main_ancestry``, and CI reported at
        least one run with every conclusion equal to ``success``.
    """
    reasons: list[str] = []
    sha = target_sha.strip().lower()
    if not _FULL_SHA.match(sha):
        reasons.append("target SHA must be a full 40-character commit hash")
    elif sha not in {candidate.strip().lower() for candidate in main_ancestry}:
        reasons.append("target SHA is not reachable from origin/main")
    conclusions = [conclusion.strip().lower() for conclusion in ci_conclusions]
    if not conclusions:
        reasons.append("no CI check runs found for target SHA")
    elif any(conclusion != "success" for conclusion in conclusions):
        reasons.append("CI is not green for target SHA")
    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))


def _run(args: list[str]) -> str:
    """Run a trusted command and return trimmed stdout, raising on failure."""
    completed = subprocess.run(args, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _main_ancestry() -> list[str]:
    """Full SHAs reachable from ``origin/main`` on the runner."""
    return _run(["git", "rev-list", "origin/main"]).splitlines()


def _ci_conclusions(repo: str, sha: str) -> list[str]:
    """CI check-run conclusions for ``sha`` via the GitHub CLI.

    Any GitHub CLI failure (unknown commit, missing auth, transient outage) is
    treated as "no runs" so the SHA fails closed as ineligible rather than
    crashing — an uncertain CI state must never promote to production.
    """
    try:
        raw = _run(
            [
                "gh",
                "api",
                f"repos/{repo}/commits/{sha}/check-runs",
                "--jq",
                ".check_runs[].conclusion",
            ]
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return raw.splitlines() if raw else []


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: verify eligibility of a target SHA; exit non-zero if ineligible."""
    parser = argparse.ArgumentParser(description="Verify production-promotion eligibility.")
    parser.add_argument("--sha", required=True, help="Target commit SHA to promote.")
    parser.add_argument("--repo", required=True, help="owner/name for the GitHub repository.")
    args = parser.parse_args(argv)

    result = evaluate_eligibility(
        target_sha=args.sha,
        main_ancestry=_main_ancestry(),
        ci_conclusions=_ci_conclusions(args.repo, args.sha),
    )
    if result.eligible:
        print(f"ELIGIBLE {args.sha}")
        return 0
    for reason in result.reasons:
        print(f"INELIGIBLE: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
