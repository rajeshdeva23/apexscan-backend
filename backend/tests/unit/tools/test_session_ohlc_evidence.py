"""DEPLOY-10 R4A tests for the current-session OHLC authority evidence tool.

Deterministic and offline: synthetic observations only, no network, no wall-clock sleeps.
Covers price classification, monotonicity, the verdict engine (ACCEPTED/REJECTED/
INCONCLUSIVE), secret sanitisation, and JSON↔Markdown rendering.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from app.tools.session_ohlc_evidence import evaluate as evaluate_module
from app.tools.session_ohlc_evidence.evaluate import (
    classify_price,
    evaluate_monotonicity,
    evaluate_record,
)
from app.tools.session_ohlc_evidence.models import (
    Classification,
    EvidenceRecord,
    InstrumentEvidence,
    LateStartEvidence,
    OhlcObservation,
    OracleComparison,
    ReconnectEvidence,
    VerdictOutcome,
)
from app.tools.session_ohlc_evidence.report import sanitize_text, to_json, to_markdown

_D = date(2026, 8, 31)
_T0 = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
_T1 = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
_TICK = Decimal("0.05")


def _ws(window: str, o: str, h: str, low: str, when: datetime) -> OhlcObservation:
    return OhlcObservation(
        source="ws",
        window=window,
        observed_at=when,
        open_price=Decimal(o),
        high_price=Decimal(h),
        low_price=Decimal(low),
    )


def _cmp(window: str, field: str, cls: Classification) -> OracleComparison:
    return OracleComparison(
        window=window,
        field=field,
        ws_value=Decimal("1"),
        rest_value=Decimal("1"),
        classification=cls,
    )


def _instrument(
    *,
    ws: tuple[OhlcObservation, ...],
    comparisons: tuple[OracleComparison, ...],
) -> InstrumentEvidence:
    return InstrumentEvidence(
        exchange="NSE",
        symbol="RELIANCE",
        security_id="2885",
        trading_date=_D,
        ws_observations=ws,
        oracle_comparisons=comparisons,
        monotonicity=evaluate_monotonicity(ws),
    )


def _record(*, instruments: tuple[InstrumentEvidence, ...], **overrides: object) -> EvidenceRecord:
    base: dict[str, object] = {
        "collector_version": "1.0.0",
        "source_sha": "160d827",
        "provider": "dhan",
        "trading_date": _D,
        "session_identity": "regular",
        "collection_start": _T0,
        "collection_end": _T1,
        "universe_expected": len(instruments),
        "universe_observed": len(instruments),
        "sample_windows": ("early", "late"),
        "instruments": instruments,
        "late_start": LateStartEvidence(
            observed=True, open_present=True, contains_prior_high=True, contains_prior_low=True
        ),
        "reconnect": ReconnectEvidence(
            observed=True,
            same_session=True,
            open_preserved=True,
            high_preserved=True,
            low_preserved=True,
        ),
        "csoa16_required": True,
        "oracle_available": True,
    }
    base.update(overrides)
    return EvidenceRecord(**base)  # type: ignore[arg-type]


def _accepted_instrument() -> InstrumentEvidence:
    ws = (_ws("early", "100", "102", "99", _T0), _ws("late", "100", "105", "98", _T1))
    comparisons = tuple(
        _cmp(w, f, Classification.MATCH) for w in ("early", "late") for f in ("open", "high", "low")
    )
    return _instrument(ws=ws, comparisons=comparisons)


# --------------------------------------------------------------------------- #
# classify_price
# --------------------------------------------------------------------------- #
def test_evid_01_exact_match() -> None:
    assert (
        classify_price(Decimal("100.00"), Decimal("100.0"), tick_size=_TICK) is Classification.MATCH
    )


def test_evid_02_one_tick_drift() -> None:
    assert (
        classify_price(Decimal("100.00"), Decimal("100.05"), tick_size=_TICK)
        is Classification.DRIFT
    )
    assert (
        classify_price(Decimal("100.00"), Decimal("100.06"), tick_size=_TICK)
        is Classification.MISMATCH
    )


def test_evid_16_missing_value_fails_closed() -> None:
    assert classify_price(None, Decimal("100"), tick_size=_TICK) is Classification.MISMATCH
    assert classify_price(Decimal("100"), None, tick_size=_TICK) is Classification.MISMATCH


def test_open_uses_exact_no_drift() -> None:
    assert classify_price(Decimal("100"), Decimal("100.05"), tick_size=_TICK, exact=True) is (
        Classification.MISMATCH
    )


# --------------------------------------------------------------------------- #
# Verdict engine
# --------------------------------------------------------------------------- #
def test_evid_01_full_evidence_accepted() -> None:
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),)))
    assert verdict.outcome is VerdictOutcome.ACCEPTED


def test_evid_02_drift_still_accepted() -> None:
    ws = (_ws("early", "100", "102", "99", _T0),)
    comparisons = (
        _cmp("early", "open", Classification.MATCH),
        _cmp("early", "high", Classification.DRIFT),
        _cmp("early", "low", Classification.MATCH),
    )
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=comparisons),)))
    assert verdict.outcome is VerdictOutcome.ACCEPTED
    assert verdict.high_low_drift == 1


def test_evid_03_open_mismatch_rejected() -> None:
    ws = (_ws("early", "100", "102", "99", _T0),)
    comparisons = (_cmp("early", "open", Classification.MISMATCH),)
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=comparisons),)))
    assert verdict.outcome is VerdictOutcome.REJECTED
    assert verdict.open_mismatches == 1


def test_evid_04_open_change_same_session_rejected() -> None:
    ws = (_ws("early", "100", "102", "99", _T0), _ws("late", "101", "103", "98", _T1))
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=()),)))
    assert verdict.outcome is VerdictOutcome.REJECTED


def test_evid_05_high_regression_rejected() -> None:
    ws = (_ws("early", "100", "105", "99", _T0), _ws("late", "100", "104", "98", _T1))
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=()),)))
    assert verdict.outcome is VerdictOutcome.REJECTED


def test_evid_06_low_regression_rejected() -> None:
    ws = (_ws("early", "100", "105", "98", _T0), _ws("late", "100", "106", "99", _T1))
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=()),)))
    assert verdict.outcome is VerdictOutcome.REJECTED


def test_evid_07_new_trading_date_baseline_is_per_record() -> None:
    # Monotonicity is computed per instrument-record (one trading_date); a fresh date is a
    # separate record and starts clean. A same-date monotonic sequence has no violation.
    ws = (_ws("early", "100", "102", "99", _T0), _ws("late", "100", "108", "95", _T1))
    result = evaluate_monotonicity(ws)
    assert result.violations == ()


def test_evid_08_late_start_retains_high_ok() -> None:
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),)))
    assert verdict.outcome is VerdictOutcome.ACCEPTED  # late_start contains prior extrema


def test_evid_09_late_start_loses_high_rejected() -> None:
    late = LateStartEvidence(
        observed=True, open_present=True, contains_prior_high=False, contains_prior_low=True
    )
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), late_start=late))
    assert verdict.outcome is VerdictOutcome.REJECTED


def test_evid_10_11_12_reconnect_preserved_ok() -> None:
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),)))
    assert verdict.outcome is VerdictOutcome.ACCEPTED  # open/high/low all preserved + same session


def test_evid_13_reconnect_resets_extrema_rejected() -> None:
    reconnect = ReconnectEvidence(
        observed=True,
        same_session=True,
        open_preserved=False,
        high_preserved=True,
        low_preserved=True,
    )
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), reconnect=reconnect))
    assert verdict.outcome is VerdictOutcome.REJECTED


def test_evid_14_no_reconnect_evidence_inconclusive() -> None:
    reconnect = ReconnectEvidence(observed=False)
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), reconnect=reconnect))
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("reconnect" in r for r in verdict.reasons)


def test_evid_15_partial_universe_inconclusive() -> None:
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), universe_expected=210))
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("partial universe" in r for r in verdict.reasons)


def test_no_instruments_inconclusive() -> None:
    verdict = evaluate_record(_record(instruments=(), universe_expected=0, universe_observed=0))
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE


def test_oracle_unavailable_inconclusive() -> None:
    verdict = evaluate_record(
        _record(instruments=(_accepted_instrument(),), oracle_available=False)
    )
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE


def test_evid_19_special_session_identity_supported() -> None:
    verdict = evaluate_record(
        _record(instruments=(_accepted_instrument(),), session_identity="muhurat_special")
    )
    assert verdict.outcome is VerdictOutcome.ACCEPTED


# --------------------------------------------------------------------------- #
# Secret safety + rendering + purity
# --------------------------------------------------------------------------- #
def test_evid_17_secret_material_redacted() -> None:
    dirty = "url=wss://api-feed.dhan.co?token=eyABC123&clientId=100 pin=123456 password=hunter2"
    clean = sanitize_text(dirty)
    for leak in ("eyABC123", "clientId=100", "123456", "hunter2"):
        assert leak not in clean
    assert "<redacted>" in clean


def test_evid_17b_render_sanitises_record_strings() -> None:
    record = _record(
        instruments=(_accepted_instrument(),),
        session_identity="regular token=eySECRETshouldnotleak",
    )
    verdict = evaluate_record(record)
    rendered = to_json(record, verdict) + to_markdown(record, verdict)
    assert "eySECRETshouldnotleak" not in rendered


def test_evid_18_evaluator_cannot_mutate_authority() -> None:
    source = inspect.getsource(evaluate_module)
    assert "tick_aggregate_verified" not in source
    assert "staged_observation_verified" not in source
    assert "SessionStatisticsAuthority" not in source


def test_evid_20_json_is_deterministic_and_round_trips() -> None:
    record = _record(instruments=(_accepted_instrument(),))
    verdict = evaluate_record(record)
    first = to_json(record, verdict)
    second = to_json(record, verdict)
    assert first == second  # deterministic (sorted keys)
    payload = json.loads(first)
    restored = EvidenceRecord.model_validate(payload["record"])
    assert restored == record  # lossless round-trip
    assert evaluate_record(restored).outcome is verdict.outcome
