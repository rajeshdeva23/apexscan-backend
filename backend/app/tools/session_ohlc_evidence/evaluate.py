"""Deterministic, offline evaluation of a current-session OHLC evidence record.

Pure functions only — no I/O, no network, no runtime-state access, and crucially no
ability to flip any authority capability (ADR-008/009 CSOA20: enablement is a governed
composition change, never a side effect of evaluation).

Conclusions are DERIVED from raw recorded observations, never trusted from asserted
booleans: late-start and reconnect continuity are computed from the stored pre/post values;
coverage is identity-based (expected vs observed instrument identities, rejecting missing,
duplicate, or unexpected); required session windows must be present; and a high/low
comparison with an unknown tick size is INDETERMINATE (→ INCONCLUSIVE), never silently DRIFT.
"""

from __future__ import annotations

from decimal import Decimal

from app.tools.session_ohlc_evidence.models import (
    Classification,
    EvidenceRecord,
    LateStartEvidence,
    MonotonicityResult,
    OhlcObservation,
    OracleComparison,
    ReconnectEvidence,
    Verdict,
    VerdictOutcome,
)


def classify_price(
    ws_value: Decimal | None,
    rest_value: Decimal | None,
    *,
    tick_size: Decimal | None,
    exact: bool = False,
) -> Classification:
    """Classify a WS value against the oracle: MATCH / DRIFT / INDETERMINATE / MISMATCH.

    A missing value on either side is a MISMATCH (fail closed). ``exact`` (the session open)
    permits only equality. Otherwise, when the values differ: an unknown ``tick_size``
    (``None``) yields INDETERMINATE (drift cannot be justified — fail safe), a difference
    within an explicit ``tick_size`` is DRIFT, and anything larger is a MISMATCH.
    """
    if ws_value is None or rest_value is None:
        return Classification.MISMATCH
    if ws_value == rest_value:
        return Classification.MATCH
    if exact:
        return Classification.MISMATCH
    if tick_size is None:
        return Classification.INDETERMINATE
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
    """Verify open-stable / high-non-decreasing / low-non-increasing over one session."""
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
    elif comparison.classification is Classification.INDETERMINATE:
        counts["hl_indeterminate"] += 1
    elif comparison.field != "open" and comparison.classification is Classification.MISMATCH:
        counts["hl_mismatch"] += 1


def _count_signals(record: EvidenceRecord) -> dict[str, int]:
    counts = {"open_mismatch": 0, "mono": 0, "hl_drift": 0, "hl_indeterminate": 0, "hl_mismatch": 0}
    for inst in record.instruments:
        for comparison in inst.oracle_comparisons:
            _tally(comparison, counts)
        if inst.monotonicity is not None:
            counts["mono"] += len(inst.monotonicity.violations)
    return counts


def _rejection_reasons(record: EvidenceRecord, counts: dict[str, int]) -> list[str]:
    """Hard-rejection reasons (open mismatch, regression, derived late-start/reconnect loss)."""
    reasons: list[str] = []
    if counts["open_mismatch"]:
        reasons.append(f"{counts['open_mismatch']} open price mismatch(es) vs oracle")
    if counts["mono"]:
        reasons.append(f"{counts['mono']} within-session monotonicity violation(s)")
    if counts["hl_mismatch"]:
        reasons.append(f"{counts['hl_mismatch']} high/low mismatch(es) beyond one-tick drift")
    reasons.extend(_late_start_rejections(record.late_start))
    reasons.extend(_reconnect_rejections(record.reconnect))
    return reasons


def _all_present(*values: Decimal | None) -> bool:
    return all(v is not None for v in values)


def _late_start_rejections(ev: LateStartEvidence | None) -> list[str]:
    if ev is None or not ev.observed:
        return []
    if not _all_present(
        ev.prior_open, ev.prior_high, ev.prior_low, ev.first_open, ev.first_high, ev.first_low
    ):
        return []  # incomplete raw evidence → handled as INCONCLUSIVE, not a rejection
    assert ev.prior_high is not None and ev.prior_low is not None  # narrowed by _all_present
    assert ev.first_high is not None and ev.first_low is not None
    lost: list[str] = []
    if ev.first_open != ev.prior_open:
        lost.append("open")
    if ev.first_high < ev.prior_high:
        lost.append("high")
    if ev.first_low > ev.prior_low:
        lost.append("low")
    return [f"late-start lost prior session extrema: {','.join(lost)}"] if lost else []


def _reconnect_rejections(ev: ReconnectEvidence | None) -> list[str]:
    if ev is None or not ev.observed or ev.pre is None or ev.post is None:
        return []
    pre, post = ev.pre, ev.post
    if not _all_present(
        pre.open_price,
        pre.high_price,
        pre.low_price,
        post.open_price,
        post.high_price,
        post.low_price,
    ):
        return []
    assert pre.high_price is not None and pre.low_price is not None  # narrowed by _all_present
    assert post.high_price is not None and post.low_price is not None
    if pre.trading_date is not None and post.trading_date != pre.trading_date:
        return ["reconnect created a new session identity (CSOA16 failure)"]
    lost: list[str] = []
    if post.open_price != pre.open_price:
        lost.append("open")
    if post.high_price < pre.high_price:
        lost.append("high")
    if post.low_price > pre.low_price:
        lost.append("low")
    return (
        [f"post-reconnect lost session-to-date extrema: {','.join(lost)} (CSOA16 failure)"]
        if lost
        else []
    )


def _coverage_reasons(record: EvidenceRecord) -> list[str]:
    if not record.instruments:
        return ["no instruments observed"]
    observed_ids = [inst.identity for inst in record.instruments]
    observed_set = set(observed_ids)
    expected_set = set(record.expected_instruments)
    reasons: list[str] = []
    if len(observed_ids) != len(observed_set):
        reasons.append("duplicate instrument observations")
    if expected_set - observed_set:
        reasons.append(f"{len(expected_set - observed_set)} expected instrument(s) not observed")
    if observed_set - expected_set:
        reasons.append(f"{len(observed_set - expected_set)} unexpected instrument(s) observed")
    return reasons


def _window_reasons(record: EvidenceRecord) -> list[str]:
    missing = set(record.required_windows) - set(record.sample_windows)
    return [f"missing required sample window(s): {sorted(missing)}"] if missing else []


def _late_start_incomplete(ev: LateStartEvidence | None) -> list[str]:
    if ev is None or not ev.observed:
        return ["no late-start evidence"]
    if not _all_present(
        ev.prior_open, ev.prior_high, ev.prior_low, ev.first_open, ev.first_high, ev.first_low
    ):
        return ["late-start evidence incomplete (missing raw prior/first values)"]
    return []


def _reconnect_incomplete(record: EvidenceRecord) -> list[str]:
    ev = record.reconnect
    if record.csoa16_required and (ev is None or not ev.observed):
        return ["no reconnect evidence for CSOA16"]
    if ev is not None and ev.observed and (ev.pre is None or ev.post is None):
        return ["reconnect evidence incomplete (missing pre/post observation)"]
    return []


def _inconclusive_reasons(record: EvidenceRecord, counts: dict[str, int]) -> list[str]:
    reasons: list[str] = []
    if not record.oracle_available:
        reasons.append("oracle unavailable")
    reasons.extend(_coverage_reasons(record))
    reasons.extend(_window_reasons(record))
    reasons.extend(_reconnect_incomplete(record))
    reasons.extend(_late_start_incomplete(record.late_start))
    if counts["hl_indeterminate"]:
        reasons.append(
            f"{counts['hl_indeterminate']} high/low comparison(s) indeterminate (tick size unknown)"
        )
    return reasons


def evaluate_record(record: EvidenceRecord) -> Verdict:
    """Return the deterministic ACCEPTED / REJECTED / INCONCLUSIVE verdict with reasons."""
    counts = _count_signals(record)
    rejections = _rejection_reasons(record, counts)
    if rejections:
        return Verdict(
            outcome=VerdictOutcome.REJECTED,
            reasons=tuple(rejections),
            open_mismatches=counts["open_mismatch"],
            monotonicity_violations=counts["mono"],
            high_low_drift=counts["hl_drift"],
            high_low_indeterminate=counts["hl_indeterminate"],
            high_low_mismatch=counts["hl_mismatch"],
        )
    inconclusive = _inconclusive_reasons(record, counts)
    if inconclusive:
        return Verdict(
            outcome=VerdictOutcome.INCONCLUSIVE,
            reasons=tuple(inconclusive),
            high_low_drift=counts["hl_drift"],
            high_low_indeterminate=counts["hl_indeterminate"],
        )
    return Verdict(
        outcome=VerdictOutcome.ACCEPTED,
        reasons=(
            "all mandatory evidence satisfied: open exact, high/low within recorded tolerance, "
            "monotonic, identity-complete coverage, all required windows, late-start extrema "
            "retained, reconnect continuity",
        ),
        high_low_drift=counts["hl_drift"],
    )
