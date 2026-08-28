"""Deterministic, offline evaluation of a current-session OHLC evidence record.

Pure functions only — no I/O, no network, no runtime-state access, and crucially no
ability to flip any authority capability (ADR-008/009 CSOA20: enablement is a governed
composition change, never a side effect of evaluation). Implements the acceptance,
rejection, and inconclusive criteria of the tick-aggregate evidence procedure.
"""

from __future__ import annotations

from decimal import Decimal

from app.tools.session_ohlc_evidence.models import (
    Classification,
    EvidenceRecord,
    MonotonicityResult,
    OhlcObservation,
    OracleComparison,
    Verdict,
    VerdictOutcome,
)


def classify_price(
    ws_value: Decimal | None,
    rest_value: Decimal | None,
    *,
    tick_size: Decimal,
    exact: bool = False,
) -> Classification:
    """Classify a WS value against the oracle: MATCH / DRIFT (<=1 tick) / MISMATCH.

    A missing value on either side is a MISMATCH (fail closed). ``exact`` (used for the
    session open) forbids any drift — only exact equality is a MATCH.
    """
    if ws_value is None or rest_value is None:
        return Classification.MISMATCH
    if ws_value == rest_value:
        return Classification.MATCH
    if exact:
        return Classification.MISMATCH
    return (
        Classification.DRIFT if abs(ws_value - rest_value) <= tick_size else Classification.MISMATCH
    )


def _open_violations(observations: tuple[OhlcObservation, ...]) -> list[str]:
    first: Decimal | None = None
    out: list[str] = []
    for obs in observations:
        if obs.open_price is None:
            continue
        if first is None:
            first = obs.open_price
        elif obs.open_price != first:
            out.append(f"open changed {first}->{obs.open_price} at {obs.window}")
    return out


def _high_violations(observations: tuple[OhlcObservation, ...]) -> list[str]:
    prev: Decimal | None = None
    out: list[str] = []
    for obs in observations:
        if obs.high_price is None:
            continue
        if prev is not None and obs.high_price < prev:
            out.append(f"high regressed {prev}->{obs.high_price} at {obs.window}")
        prev = obs.high_price if prev is None else max(prev, obs.high_price)
    return out


def _low_violations(observations: tuple[OhlcObservation, ...]) -> list[str]:
    prev: Decimal | None = None
    out: list[str] = []
    for obs in observations:
        if obs.low_price is None:
            continue
        if prev is not None and obs.low_price > prev:
            out.append(f"low regressed {prev}->{obs.low_price} at {obs.window}")
        prev = obs.low_price if prev is None else min(prev, obs.low_price)
    return out


def evaluate_monotonicity(observations: tuple[OhlcObservation, ...]) -> MonotonicityResult:
    """Verify open-stable / high-non-decreasing / low-non-increasing over one session.

    Observations must belong to a single trading date (the caller resets the baseline at
    a new session). ``None`` prices are skipped for the comparison but never treated as a
    regression.
    """
    open_v = _open_violations(observations)
    high_v = _high_violations(observations)
    low_v = _low_violations(observations)
    return MonotonicityResult(
        open_stable=not open_v,
        high_non_decreasing=not high_v,
        low_non_increasing=not low_v,
        violations=tuple(open_v + high_v + low_v),
    )


def _tally(comparison: OracleComparison, counts: dict[str, int]) -> None:
    if comparison.field == "open" and comparison.classification is Classification.MISMATCH:
        counts["open_mismatch"] += 1
    elif comparison.classification is Classification.DRIFT:
        counts["hl_drift"] += 1
    elif comparison.field != "open" and comparison.classification is Classification.MISMATCH:
        counts["hl_mismatch"] += 1


def _count_signals(record: EvidenceRecord) -> dict[str, int]:
    counts = {"open_mismatch": 0, "mono": 0, "hl_drift": 0, "hl_mismatch": 0}
    for inst in record.instruments:
        for comparison in inst.oracle_comparisons:
            _tally(comparison, counts)
        if inst.monotonicity is not None:
            counts["mono"] += len(inst.monotonicity.violations)
    return counts


def _rejection_reasons(record: EvidenceRecord) -> tuple[list[str], dict[str, int]]:
    """Collect hard-rejection reasons (open mismatch, regression, late-start/reconnect loss)."""
    reasons: list[str] = []
    counts = _count_signals(record)
    if counts["open_mismatch"]:
        reasons.append(f"{counts['open_mismatch']} open price mismatch(es) vs oracle")
    if counts["mono"]:
        reasons.append(f"{counts['mono']} within-session monotonicity violation(s)")
    if counts["hl_mismatch"]:
        reasons.append(f"{counts['hl_mismatch']} high/low mismatch(es) beyond one-tick drift")
    reasons.extend(_late_start_rejections(record))
    reasons.extend(_reconnect_rejections(record))
    return reasons, counts


def _late_start_rejections(record: EvidenceRecord) -> list[str]:
    ev = record.late_start
    if ev is None or not ev.observed:
        return []
    if (
        ev.contains_prior_high is False
        or ev.contains_prior_low is False
        or ev.open_present is False
    ):
        return ["late-start observation lost a prior session extremum (open/high/low)"]
    return []


def _reconnect_rejections(record: EvidenceRecord) -> list[str]:
    ev = record.reconnect
    if ev is None or not ev.observed:
        return []
    if ev.open_preserved is False or ev.high_preserved is False or ev.low_preserved is False:
        return ["post-reconnect aggregate lost session-to-date extrema (CSOA16 failure)"]
    if ev.same_session is False:
        return ["reconnect created a new session identity (CSOA16 failure)"]
    return []


def _inconclusive_reasons(record: EvidenceRecord) -> list[str]:
    """Collect reasons the evidence cannot yet reach ACCEPTED (coverage / missing evidence)."""
    reasons: list[str] = []
    if not record.oracle_available:
        reasons.append("oracle unavailable")
    if record.universe_observed == 0:
        reasons.append("no instruments observed")
    elif record.universe_observed < record.universe_expected:
        reasons.append(
            f"partial universe coverage {record.universe_observed}/{record.universe_expected}"
        )
    if record.csoa16_required and (record.reconnect is None or not record.reconnect.observed):
        reasons.append("no reconnect evidence for CSOA16")
    if record.late_start is None or not record.late_start.observed:
        reasons.append("no late-start evidence")
    return reasons


def evaluate_record(record: EvidenceRecord) -> Verdict:
    """Return the deterministic ACCEPTED / REJECTED / INCONCLUSIVE verdict with reasons."""
    rejections, counts = _rejection_reasons(record)
    if rejections:
        return Verdict(
            outcome=VerdictOutcome.REJECTED,
            reasons=tuple(rejections),
            open_mismatches=counts["open_mismatch"],
            monotonicity_violations=counts["mono"],
            high_low_drift=counts["hl_drift"],
            high_low_mismatch=counts["hl_mismatch"],
        )
    inconclusive = _inconclusive_reasons(record)
    if inconclusive:
        return Verdict(
            outcome=VerdictOutcome.INCONCLUSIVE,
            reasons=tuple(inconclusive),
            high_low_drift=counts["hl_drift"],
        )
    return Verdict(
        outcome=VerdictOutcome.ACCEPTED,
        reasons=(
            "all mandatory evidence satisfied: open exact, high/low within tolerance, "
            "monotonic, late-start retained, reconnect continuity, full coverage",
        ),
        high_low_drift=counts["hl_drift"],
    )
