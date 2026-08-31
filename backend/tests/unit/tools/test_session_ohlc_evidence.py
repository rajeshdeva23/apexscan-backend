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

import pytest

from app.tools.session_ohlc_evidence import evaluate as evaluate_module
from app.tools.session_ohlc_evidence.canonical import (
    float32_equivalent,
    float32_hex,
    is_finite_price,
)
from app.tools.session_ohlc_evidence.evaluate import (
    classify_price,
    combine_records,
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


# --------------------------------------------------------------------------- #
# PREC — float32 protocol-equivalence canonicalization (R4C)
# --------------------------------------------------------------------------- #
# The R4B REJECTED artifact: WS decodes float32 wire prices via Decimal(str(float)), widening
# them to their full binary64 expansion, while REST returns clean 2dp Decimals. These are the
# SAME price and must not be treated as a mismatch — but WITHOUT rounding to 2dp, without a
# universal precision, and without a universal tick.
def test_prec_01_float32_noise_is_protocol_equivalent() -> None:
    # 212.3699951171875 is exactly the binary64 expansion of float32(212.37).
    assert (
        classify_price(Decimal("212.3699951171875"), Decimal("212.37"), tick_size=None, exact=True)
        is Classification.PROTOCOL_EQUIVALENT
    )


def test_prec_02_second_real_r4b_case() -> None:
    assert (
        classify_price(Decimal("402.04998779296875"), Decimal("402.05"), tick_size=None, exact=True)
        is Classification.PROTOCOL_EQUIVALENT
    )


def test_prec_03_symmetric() -> None:
    assert float32_equivalent(Decimal("212.37"), Decimal("212.3699951171875"))
    assert float32_equivalent(Decimal("212.3699951171875"), Decimal("212.37"))


def test_prec_04_genuinely_distinct_prices_stay_mismatch() -> None:
    # Two different real prices must not collapse to equivalent.
    assert not float32_equivalent(Decimal("212.37"), Decimal("212.42"))
    assert (
        classify_price(Decimal("212.37"), Decimal("212.42"), tick_size=None, exact=True)
        is Classification.MISMATCH
    )


def test_prec_05_not_arbitrary_2dp_rounding() -> None:
    # 100.001 and 100.002 both round to 100.00 at 2dp but are NOT float32-equivalent:
    # canonicalization is representation-based, not decimal-rounding-based.
    assert not float32_equivalent(Decimal("100.001"), Decimal("100.002"))


def test_prec_06_one_paisa_apart_not_equivalent() -> None:
    # No universal tick is assumed: a genuine 0.01 difference is not canonicalized away.
    assert not float32_equivalent(Decimal("212.37"), Decimal("212.38"))
    assert float32_hex(Decimal("212.37")) != float32_hex(Decimal("212.38"))


def test_prec_07_non_finite_rejected_no_canonical_form() -> None:
    assert float32_hex(Decimal("NaN")) is None
    assert float32_hex(Decimal("Infinity")) is None
    assert not is_finite_price(Decimal("NaN"))
    assert not is_finite_price(Decimal("Infinity"))


def test_prec_08_non_finite_classifies_mismatch() -> None:
    assert (
        classify_price(Decimal("NaN"), Decimal("212.37"), tick_size=None) is Classification.MISMATCH
    )


def test_prec_09_none_has_no_bits_and_is_not_equivalent() -> None:
    assert float32_hex(None) is None
    assert not float32_equivalent(None, Decimal("212.37"))


def test_prec_10_protocol_equivalent_open_is_accepted_and_counted() -> None:
    ws = (_ws("early", "212.3699951171875", "215.0", "210.0", _T0),)
    comparisons = (
        _cmp("early", "open", Classification.PROTOCOL_EQUIVALENT),
        _cmp("early", "high", Classification.MATCH),
        _cmp("early", "low", Classification.MATCH),
    )
    verdict = evaluate_record(_record(instruments=(_instrument(ws=ws, comparisons=comparisons),)))
    assert verdict.outcome is VerdictOutcome.ACCEPTED
    assert verdict.protocol_equivalent == 1
    assert verdict.open_mismatches == 0


# --------------------------------------------------------------------------- #
# COV — identity/pending coverage (R4C)
# --------------------------------------------------------------------------- #
def test_cov_01_pending_surfaces_missing_identities_inconclusive() -> None:
    verdict = evaluate_record(
        _record(
            instruments=(_accepted_instrument(),),
            expected_instruments=("NSE:RELIANCE", "NSE:TCS", "NSE:INFY"),
            pending_instruments=("NSE:INFY", "NSE:TCS"),
        )
    )
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("not observed" in r for r in verdict.reasons)


def test_cov_02_full_identity_coverage_accepted() -> None:
    assert (
        evaluate_record(_record(instruments=(_accepted_instrument(),))).outcome
        is VerdictOutcome.ACCEPTED
    )


def test_cov_03_unexpected_identity_not_accepted() -> None:
    verdict = evaluate_record(
        _record(instruments=(_accepted_instrument(),), expected_instruments=())
    )
    assert verdict.outcome is not VerdictOutcome.ACCEPTED
    assert any("unexpected" in r for r in verdict.reasons)


def test_cov_04_render_reports_pending_count() -> None:
    record = _record(
        instruments=(_accepted_instrument(),),
        expected_instruments=("NSE:RELIANCE", "NSE:TCS"),
        pending_instruments=("NSE:TCS",),
    )
    md = to_markdown(record, evaluate_record(record))
    assert "pending: 1" in md


# --------------------------------------------------------------------------- #
# WIN — resumable multi-window combine (R4C)
# --------------------------------------------------------------------------- #
def _window_record(window: str, when: datetime, **overrides: object) -> EvidenceRecord:
    inst = _instrument(
        ws=(_ws(window, "100", "102", "99", when),),
        comparisons=(
            _cmp(window, "open", Classification.MATCH),
            _cmp(window, "high", Classification.MATCH),
            _cmp(window, "low", Classification.MATCH),
        ),
    )
    return _record(
        instruments=(inst,),
        sample_windows=(window,),
        collection_start=when,
        collection_end=when,
        **overrides,
    )


def test_win_01_combine_merges_windows_to_full_coverage() -> None:
    parts = [
        _window_record("early", _T0),
        _window_record("mid", _T0),
        _window_record("late", _T1),
    ]
    combined = combine_records(parts)
    assert combined.sample_windows == ("early", "late", "mid")
    assert len(combined.instruments) == 1
    assert len(combined.instruments[0].oracle_comparisons) == 9


def test_win_02_combined_record_evaluates_accepted() -> None:
    parts = [
        _window_record("early", _T0),
        _window_record("mid", _T0),
        _window_record("late", _T1),
    ]
    assert evaluate_record(combine_records(parts)).outcome is VerdictOutcome.ACCEPTED


def test_win_03_combine_rejects_cross_trading_date() -> None:
    a = _window_record("early", _T0)
    b = _window_record("late", _T1, trading_date=date(2026, 9, 1))
    with pytest.raises(ValueError, match="different trading_date"):
        combine_records([a, b])


def test_win_04_combine_rejects_cross_source_sha() -> None:
    a = _window_record("early", _T0)
    b = _window_record("late", _T1, source_sha="deadbee")
    with pytest.raises(ValueError, match="different"):
        combine_records([a, b])


def test_win_05_combine_recomputes_pending_from_union() -> None:
    early = _window_record("early", _T0, expected_instruments=("NSE:RELIANCE", "NSE:TCS"))
    combined = combine_records([early])
    assert combined.pending_instruments == ("NSE:TCS",)


def test_win_06_combine_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        combine_records([])


# --------------------------------------------------------------------------- #
# CSOA16 — late-start / reconnect continuity carried through combine (R4C)
# --------------------------------------------------------------------------- #
def test_csoa16_01_combine_carries_observed_late_start() -> None:
    early = _window_record("early", _T0, late_start=LateStartEvidence(observed=False))
    late = _window_record("late", _T1, late_start=_good_late())
    combined = combine_records([early, late])
    assert combined.late_start is not None and combined.late_start.observed


def test_csoa16_02_combine_carries_observed_reconnect() -> None:
    early = _window_record("early", _T0, reconnect=ReconnectEvidence(observed=False))
    late = _window_record("late", _T1, reconnect=_good_reconnect())
    combined = combine_records([early, late])
    assert combined.reconnect is not None and combined.reconnect.observed


def test_csoa16_03_combined_reconnect_loss_rejected() -> None:
    # Realistic resumable capture: only the late window observed a reconnect, and it lost the
    # session-to-date low. The merge must surface that loss, not the earlier good windows.
    bad_post = _good_reconnect().post.model_copy(update={"low_price": Decimal("99")})  # type: ignore[union-attr]
    reconnect = _good_reconnect().model_copy(update={"post": bad_post})
    early = _window_record("early", _T0, reconnect=ReconnectEvidence(observed=False))
    mid = _window_record("mid", _T0, reconnect=ReconnectEvidence(observed=False))
    late = _window_record("late", _T1, reconnect=reconnect)
    assert evaluate_record(combine_records([early, mid, late])).outcome is VerdictOutcome.REJECTED


def test_csoa16_05_span_detects_loss_across_two_reconnects() -> None:
    # Two observed reconnects with distinct timestamps: the earliest pre → latest post span
    # catches a low regression even though each event's own record looks locally fine.
    pre_early = OhlcObservation(
        source="ws",
        window="early",
        observed_at=_T0,
        trading_date=_D,
        open_price=Decimal("100"),
        high_price=Decimal("104"),
        low_price=Decimal("95"),
    )
    post_late = OhlcObservation(
        source="ws",
        window="late",
        observed_at=_T1,
        trading_date=_D,
        open_price=Decimal("100"),
        high_price=Decimal("106"),
        low_price=Decimal("97"),  # session low rose 95 -> 97 across the span (loss)
    )
    r1 = ReconnectEvidence(observed=True, pre=pre_early, post=pre_early)
    r2 = ReconnectEvidence(observed=True, pre=post_late, post=post_late)
    early = _window_record("early", _T0, reconnect=r1)
    late = _window_record("late", _T1, reconnect=r2)
    assert evaluate_record(combine_records([early, late])).outcome is VerdictOutcome.REJECTED


def test_csoa16_04_combine_missing_reconnect_inconclusive() -> None:
    early = _window_record("early", _T0, reconnect=ReconnectEvidence(observed=False))
    mid = _window_record("mid", _T0, reconnect=ReconnectEvidence(observed=False))
    late = _window_record("late", _T1, reconnect=ReconnectEvidence(observed=False))
    verdict = evaluate_record(combine_records([early, mid, late]))
    assert verdict.outcome is VerdictOutcome.INCONCLUSIVE
    assert any("reconnect" in r for r in verdict.reasons)
