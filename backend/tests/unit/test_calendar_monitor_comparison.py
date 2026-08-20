"""Pure comparison semantics for the secondary calendar monitor (ADR-011).

Proves the exact date-level classification against the authoritative dataset and the
governance invariant that comparison NEVER mutates the dataset, the trading calendar, or
creates an authority override from observed (secondary) timing.
"""

from __future__ import annotations

import json
from datetime import date, time

from app.adapters.dhan.calendar_monitor_parser import (
    CalendarObservationParseStatus,
    DhanCalendarObservation,
    ObservedCalendarDate,
    ObservedDateStatus,
    ObservedInterval,
)
from app.market_engine.calendar_data import TradingCalendarDataset, load_nse_cm_2026_dataset
from app.services.calendar_monitor import CalendarComparisonStatus, compare


def _closed(day: date) -> ObservedCalendarDate:
    return ObservedCalendarDate(observed_date=day, status=ObservedDateStatus.CLOSED)


def _open(day: date, intervals: tuple[ObservedInterval, ...] = ()) -> ObservedCalendarDate:
    return ObservedCalendarDate(
        observed_date=day, status=ObservedDateStatus.OPEN, intervals=intervals
    )


def _observation(*dates: ObservedCalendarDate) -> DhanCalendarObservation:
    return DhanCalendarObservation(
        source="test",
        source_year=None,
        parse_status=CalendarObservationParseStatus.OK,
        dates=tuple(dates),
    )


def _dataset(
    *,
    closed: tuple[str, ...] = (),
    open_sessions: tuple[str, ...] = (),
) -> TradingCalendarDataset:
    governed = " ".join((*closed, *open_sessions)) or "2026-06-15"
    return TradingCalendarDataset.model_validate_json(
        json.dumps(
            {
                "dataset_id": "test",
                "version": "1.0",
                "segment": "NSE_EQ",
                "coverage_start": "2026-01-01",
                "coverage_end": "2026-12-31",
                "closed_dates": list(closed),
                "open_sessions": list(open_sessions),
                "session_overrides": [],
                "provenance": [
                    {
                        "circular_id": "X",
                        "circular_date": "2025-12-01",
                        "segment": "Capital Market",
                        "fact": f"governed dates {governed}",
                    }
                ],
            }
        )
    )


# --------------------------------------------------------------------------- #
# K–T
# --------------------------------------------------------------------------- #
def test_k_exact_agreement_is_match() -> None:
    dataset = load_nse_cm_2026_dataset()
    observation = _observation(
        _closed(date(2026, 1, 26)),  # authority closed date
        _open(date(2026, 2, 1), (ObservedInterval(start=time(9, 15), end=time(15, 30)),)),
        _closed(date(2026, 6, 27)),  # ordinary Saturday: both non-trading
        _open(date(2026, 11, 8)),  # open session, no timing to compare
    )
    comparison = compare(observation, dataset)
    assert comparison.overall_status is CalendarComparisonStatus.MATCH
    assert comparison.difference_count == 0
    assert comparison.signature == "match"


def test_l_new_dhan_closed_date_on_ordinary_weekday() -> None:
    dataset = load_nse_cm_2026_dataset()
    comparison = compare(_observation(_closed(date(2026, 6, 25))), dataset)  # a Thursday
    assert comparison.overall_status is CalendarComparisonStatus.DHAN_NEW_CLOSED_DATE
    assert comparison.difference_count == 1


def test_m_new_dhan_open_date_on_weekend() -> None:
    dataset = load_nse_cm_2026_dataset()
    comparison = compare(_observation(_open(date(2026, 6, 27))), dataset)  # a Saturday
    assert comparison.overall_status is CalendarComparisonStatus.DHAN_NEW_OPEN_DATE


def test_n_authority_explicit_open_vs_dhan_closed_conflicts() -> None:
    dataset = _dataset(open_sessions=("2026-02-01",))
    comparison = compare(_observation(_closed(date(2026, 2, 1))), dataset)
    assert comparison.overall_status is CalendarComparisonStatus.DHAN_DATE_STATUS_CONFLICT


def test_o_authority_explicit_closed_vs_dhan_open_conflicts() -> None:
    dataset = _dataset(closed=("2026-03-03",))  # a Tuesday closure
    comparison = compare(_observation(_open(date(2026, 3, 3))), dataset)
    assert comparison.overall_status is CalendarComparisonStatus.DHAN_DATE_STATUS_CONFLICT


def test_p_timing_difference_with_authority_override() -> None:
    dataset = load_nse_cm_2026_dataset()  # 2026-02-01 override is 09:15-15:30
    observation = _observation(
        _open(date(2026, 2, 1), (ObservedInterval(start=time(10, 0), end=time(14, 0)),))
    )
    comparison = compare(observation, dataset)
    assert comparison.overall_status is CalendarComparisonStatus.DHAN_SESSION_TIMING_CHANGE


def test_q_date_outside_coverage_is_missing() -> None:
    dataset = load_nse_cm_2026_dataset()
    comparison = compare(_observation(_closed(date(2027, 1, 1))), dataset)
    assert comparison.overall_status is CalendarComparisonStatus.AUTHORITATIVE_COVERAGE_MISSING


def test_r_muhurat_observed_timing_without_override_is_timing_change_and_no_mutation() -> None:
    dataset = load_nse_cm_2026_dataset()  # 2026-11-08 is OPEN with NO override
    before = dataset.model_dump()
    observation = _observation(
        _open(date(2026, 11, 8), (ObservedInterval(start=time(18, 0), end=time(19, 0)),))
    )
    comparison = compare(observation, dataset)
    assert comparison.overall_status is CalendarComparisonStatus.DHAN_SESSION_TIMING_CHANGE
    # No authority override was created from the secondary observation.
    assert date(2026, 11, 8) not in {spec.trading_date for spec in dataset.session_overrides}
    assert dataset.model_dump() == before  # dataset byte-for-byte unchanged


def test_s_comparison_does_not_mutate_dataset() -> None:
    dataset = load_nse_cm_2026_dataset()
    before = dataset.model_dump()
    compare(
        _observation(
            _closed(date(2026, 6, 25)),
            _open(date(2026, 6, 27)),
            _closed(date(2027, 1, 1)),
        ),
        dataset,
    )
    assert dataset.model_dump() == before


def test_t_comparison_does_not_mutate_trading_calendar() -> None:
    dataset = load_nse_cm_2026_dataset()
    calendar = dataset.trading_calendar()
    closed_before = frozenset(calendar.closed_dates)
    open_before = frozenset(calendar.open_sessions)
    compare(_observation(_closed(date(2026, 6, 25)), _open(date(2026, 6, 27))), dataset)
    assert frozenset(dataset.trading_calendar().closed_dates) == closed_before
    assert frozenset(dataset.trading_calendar().open_sessions) == open_before
