"""DEPLOY-10 R4A tests for the current-session OHLC authority evidence tool.

Deterministic and offline: synthetic observations only, no network, no wall-clock sleeps.
Covers price classification (incl. unknown-tick INDETERMINATE), monotonicity, derived
late-start / reconnect continuity, identity-based coverage, required-window coverage, the
verdict engine, secret sanitisation, and JSON↔Markdown rendering.
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
_WINDOWS = ("early", "mid", "late")


def _ws(window: str, o: str, h: str, low: str, when: datetime) -> OhlcObservation:
    return OhlcObservation(
        source="ws",
        window=window,
        observed_at=when,
        trading_date=_D,
        open_price=Decimal(o),
        high_price=Decimal(h),
        low_price=Decimal(low),
    )


def _cmp(
    window: str, field: str, cls: Classification, tick: Decimal | None = _TICK
) -> OracleComparison:
    return OracleComparison(
        window=window,
        field=field,
        ws_value=Decimal("1"),
        rest_value=Decimal("1"),
        tick_size=None if field == "open" else tick,
        classification=cls,
    )


def _instrument(
    *,
    symbol: str = "RELIANCE",
    ws: tuple[OhlcObservation, ...],
    comparisons: tuple[OracleComparison, ...],
) -> InstrumentEvidence:
    return InstrumentEvidence(
        exchange="NSE",
        symbol=symbol,
        security_id="2885",
        trading_date=_D,
        ws_observations=ws,
        oracle_comparisons=comparisons,
        monotonicity=evaluate_monotonicity(ws),
    )


def _good_late() -> LateStartEvidence:
    return LateStartEvidence(
        observed=True,
        prior_observed_at=_T0,
        prior_open=Decimal("100"),
        prior_high=Decimal("104"),
        prior_low=Decimal("97"),
        first_observed_at=_T1,
        first_open=Decimal("100"),
        first_high=Decimal("105"),
        first_low=Decimal("96"),
    )


def _good_reconnect() -> ReconnectEvidence:
    return ReconnectEvidence(
        observed=True,
        pre=OhlcObservation(
            source="ws",
            window="mid",
            observed_at=_T0,
            trading_date=_D,
            open_price=Decimal("100"),
            high_price=Decimal("104"),
            low_price=Decimal("97"),
        ),
        post=OhlcObservation(
            source="ws",
            window="late",
            observed_at=_T1,
            trading_date=_D,
            open_price=Decimal("100"),
            high_price=Decimal("106"),
            low_price=Decimal("95"),
        ),
    )


def _record(*, instruments: tuple[InstrumentEvidence, ...], **overrides: object) -> EvidenceRecord:
    base: dict[str, object] = {
        "collector_version": "2.0.0",
        "source_sha": "105c9c3",
        "provider": "dhan",
        "trading_date": _D,
        "session_identity": "regular",
        "collection_start": _T0,
        "collection_end": _T1,
        "expected_instruments": tuple(i.identity for i in instruments),
        "required_windows": _WINDOWS,
        "sample_windows": _WINDOWS,
        "instruments": instruments,
        "late_start": _good_late(),
        "reconnect": _good_reconnect(),
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
        classify_price(Decimal("100"), Decimal("100.05"), tick_size=_TICK) is Classification.DRIFT
    )
    assert (
        classify_price(Decimal("100"), Decimal("100.06"), tick_size=_TICK)
        is Classification.MISMATCH
    )


def test_unknown_tick_is_indeterminate_not_drift() -> None:
    assert classify_price(Decimal("100"), Decimal("100.05"), tick_size=None) is (
        Classification.INDETERMINATE
    )
    assert classify_price(Decimal("100"), Decimal("100"), tick_size=None) is Classification.MATCH


def test_evid_16_missing_value_fails_closed() -> None:
    assert classify_price(None, Decimal("100"), tick_size=_TICK) is Classification.MISMATCH


def test_open_uses_exact_no_drift() -> None:
    assert classify_price(Decimal("100"), Decimal("100.05"), tick_size=_TICK, exact=True) is (
        Classification.MISMATCH
    )


# --------------------------------------------------------------------------- #
# Verdict engine
# --------------------------------------------------------------------------- #
def test_evid_01_full_evidence_accepted() -> None:
    assert evaluate_record(_record(instruments=(_accepted_instrument(),))).outcome is (
        VerdictOutcome.ACCEPTED
    )


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


def test_unknown_tick_high_is_inconclusive() -> None:
    ws = (_ws("early", "100", "102", "99", _T0),)
    comparisons = (
        _cmp("early", "open", Classification.MATCH),
        _cmp("early", "high", Classification.INDETERMINATE, tick=None),
        _cmp("early", "low", Classification.MATCH),
    )
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=comparisons),)))
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("indeterminate" in r for r in verdict.reasons)


def test_evid_03_open_mismatch_rejected() -> None:
    ws = (_ws("early", "100", "102", "99", _T0),)
    comparisons = (_cmp("early", "open", Classification.MISMATCH),)
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=comparisons),)))
    assert verdict.outcome is VerdictOutcome.REJECTED
    assert verdict.open_mismatches == 1


def test_evid_04_open_change_same_session_rejected() -> None:
    ws = (_ws("early", "100", "102", "99", _T0), _ws("late", "101", "103", "98", _T1))
    assert evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=()),))).outcome is (
        VerdictOutcome.REJECTED
    )


def test_evid_05_high_regression_rejected() -> None:
    ws = (_ws("early", "100", "105", "99", _T0), _ws("late", "100", "104", "98", _T1))
    assert evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=()),))).outcome is (
        VerdictOutcome.REJECTED
    )


def test_evid_06_low_regression_rejected() -> None:
    ws = (_ws("early", "100", "105", "98", _T0), _ws("late", "100", "106", "99", _T1))
    assert evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=()),))).outcome is (
        VerdictOutcome.REJECTED
    )


def test_evid_07_new_trading_date_baseline_is_per_record() -> None:
    ws = (_ws("early", "100", "102", "99", _T0), _ws("late", "100", "108", "95", _T1))
    assert evaluate_monotonicity(ws).violations == ()


def test_evid_08_late_start_retains_extrema_ok() -> None:
    assert evaluate_record(_record(instruments=(_accepted_instrument(),))).outcome is (
        VerdictOutcome.ACCEPTED
    )


def test_evid_09_late_start_loses_high_rejected() -> None:
    late = _good_late().model_copy(update={"first_high": Decimal("103")})  # < prior_high 104
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), late_start=late))
    assert verdict.outcome is VerdictOutcome.REJECTED
    assert any("late-start lost" in r for r in verdict.reasons)


def test_late_start_incomplete_is_inconclusive() -> None:
    late = LateStartEvidence(observed=True, prior_high=Decimal("104"))  # missing most raw values
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), late_start=late))
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE


def test_evid_10_11_12_reconnect_preserved_ok() -> None:
    assert evaluate_record(_record(instruments=(_accepted_instrument(),))).outcome is (
        VerdictOutcome.ACCEPTED
    )


def test_evid_13_reconnect_resets_open_rejected_derived() -> None:
    bad_post = _good_reconnect().post.model_copy(update={"open_price": Decimal("101")})  # type: ignore[union-attr]
    reconnect = _good_reconnect().model_copy(update={"post": bad_post})
    verdict = evaluate_record(_record(instruments=(_accepted_instrument(),), reconnect=reconnect))
    assert verdict.outcome is VerdictOutcome.REJECTED  # derived from pre/post, not a boolean


def test_reconnect_high_regression_rejected_derived() -> None:
    bad_post = _good_reconnect().post.model_copy(update={"high_price": Decimal("103")})  # type: ignore[union-attr]
    reconnect = _good_reconnect().model_copy(update={"post": bad_post})
    assert (
        evaluate_record(_record(instruments=(_accepted_instrument(),), reconnect=reconnect)).outcome
        is VerdictOutcome.REJECTED
    )


def test_evid_14_no_reconnect_evidence_inconclusive() -> None:
    verdict = evaluate_record(
        _record(instruments=(_accepted_instrument(),), reconnect=ReconnectEvidence(observed=False))
    )
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("reconnect" in r for r in verdict.reasons)


def test_evid_15_partial_universe_inconclusive() -> None:
    verdict = evaluate_record(
        _record(
            instruments=(_accepted_instrument(),), expected_instruments=("NSE:RELIANCE", "NSE:TCS")
        )
    )
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("not observed" in r for r in verdict.reasons)


def test_evid_21_count_matches_but_identity_missing_and_duplicate_not_accepted() -> None:
    a = _instrument(
        symbol="AAA",
        ws=(_ws("early", "100", "100", "99", _T0),),
        comparisons=(_cmp("early", "open", Classification.MATCH),),
    )
    dup = _instrument(
        symbol="AAA",
        ws=(_ws("late", "100", "100", "98", _T1),),
        comparisons=(_cmp("late", "open", Classification.MATCH),),
    )
    # expected two distinct (AAA, BBB), observed count == 2 but they are AAA twice, BBB missing.
    verdict = evaluate_record(
        _record(instruments=(a, dup), expected_instruments=("NSE:AAA", "NSE:BBB"))
    )
    assert verdict.outcome is not VerdictOutcome.ACCEPTED
    assert any("duplicate" in r for r in verdict.reasons)


def test_missing_required_window_inconclusive() -> None:
    for missing in ("early", "mid", "late"):
        windows = tuple(w for w in _WINDOWS if w != missing)
        verdict = evaluate_record(
            _record(instruments=(_accepted_instrument(),), sample_windows=windows)
        )
        assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
        assert any("required sample window" in r for r in verdict.reasons)


def test_no_instruments_inconclusive() -> None:
    assert (
        evaluate_record(_record(instruments=(), expected_instruments=("NSE:AAA",))).outcome
        is VerdictOutcome.INCONCLUSIVE
    )


def test_oracle_unavailable_inconclusive() -> None:
    assert (
        evaluate_record(
            _record(instruments=(_accepted_instrument(),), oracle_available=False)
        ).outcome
        is VerdictOutcome.INCONCLUSIVE
    )


def test_evid_19_special_session_identity_supported() -> None:
    assert (
        evaluate_record(
            _record(instruments=(_accepted_instrument(),), session_identity="muhurat_special")
        ).outcome
        is VerdictOutcome.ACCEPTED
    )


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
    rendered = to_json(record, evaluate_record(record)) + to_markdown(
        record, evaluate_record(record)
    )
    assert "eySECRETshouldnotleak" not in rendered


def test_report_states_oracle_not_independent() -> None:
    record = _record(instruments=(_accepted_instrument(),))
    md = to_markdown(record, evaluate_record(record))
    assert "not independent verification" in md


def test_evid_18_evaluator_cannot_mutate_authority() -> None:
    source = inspect.getsource(evaluate_module)
    for token in (
        "tick_aggregate_verified",
        "staged_observation_verified",
        "SessionStatisticsAuthority",
    ):
        assert token not in source


def test_evid_20_json_is_deterministic_and_round_trips() -> None:
    record = _record(instruments=(_accepted_instrument(),))
    verdict = evaluate_record(record)
    assert to_json(record, verdict) == to_json(record, verdict)
    restored = EvidenceRecord.model_validate(json.loads(to_json(record, verdict))["record"])
    assert restored == record
    assert evaluate_record(restored).outcome is verdict.outcome
