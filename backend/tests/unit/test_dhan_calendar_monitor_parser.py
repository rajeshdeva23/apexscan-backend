"""Secondary Dhan calendar-monitor parser matrix (ADR-011; observation-only).

Every case uses a synthetic HTML string — no network. Proves NSE cash-equity filtering,
CLOSED/OPEN recognition, exact (never-collapsed) interval parsing, and fail-closed
behaviour on malformed/ambiguous/duplicate input.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, time

from app.adapters.dhan.calendar_monitor_parser import (
    CalendarObservationParseStatus,
    DhanMarketHolidayParser,
    ObservedDateStatus,
)

_HEADERS = ("Date", "Segment", "Status", "Timings")


def _table(rows: Sequence[Sequence[str]], *, headers: Sequence[str] = _HEADERS) -> str:
    head = "".join(f"<th>{cell}</th>" for cell in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<html><body><table><tr>{head}</tr>{body}</table></body></html>"


def _parse(html: str) -> object:
    return DhanMarketHolidayParser().parse(html, source="test")


# --------------------------------------------------------------------------- #
# A–G: successful recognition
# --------------------------------------------------------------------------- #
def test_a_valid_nse_closed_date_parsed() -> None:
    observation = _parse(_table([("2026-01-26", "NSE Equity", "Closed", "")]))
    assert observation.parse_status is CalendarObservationParseStatus.OK
    assert len(observation.dates) == 1
    entry = observation.dates[0]
    assert entry.observed_date == date(2026, 1, 26)
    assert entry.status is ObservedDateStatus.CLOSED
    assert entry.intervals == ()
    assert observation.source_year == 2026


def test_b_mcx_only_row_ignored() -> None:
    observation = _parse(
        _table([("2026-01-26", "NSE Equity", "Closed", ""), ("2026-01-27", "MCX", "Closed", "")])
    )
    assert observation.parse_status is CalendarObservationParseStatus.OK
    assert [entry.observed_date for entry in observation.dates] == [date(2026, 1, 26)]


def test_c_clearing_and_currency_only_rows_ignored() -> None:
    observation = _parse(
        _table(
            [
                ("2026-01-26", "NSE Equity", "Closed", ""),
                ("2026-01-27", "NSE Clearing", "Closed", ""),
                ("2026-01-28", "Currency Derivatives", "Closed", ""),
            ]
        )
    )
    assert observation.parse_status is CalendarObservationParseStatus.OK
    assert [entry.observed_date for entry in observation.dates] == [date(2026, 1, 26)]


def test_d_weekend_row_normalized_to_closed() -> None:
    observation = _parse(_table([("2026-06-27", "NSE", "Closed", "")]))  # a Saturday
    assert observation.parse_status is CalendarObservationParseStatus.OK
    assert observation.dates[0].status is ObservedDateStatus.CLOSED


def test_e_muhurat_open_marker_becomes_open() -> None:
    observation = _parse(_table([("2026-11-08", "NSE Equity", "Muhurat", "")]))
    assert observation.parse_status is CalendarObservationParseStatus.OK
    assert observation.dates[0].status is ObservedDateStatus.OPEN


def test_f_open_without_timing_has_empty_intervals() -> None:
    observation = _parse(_table([("2026-11-08", "NSE Equity", "Open", "")]))
    assert observation.dates[0].status is ObservedDateStatus.OPEN
    assert observation.dates[0].intervals == ()  # never fabricated


def test_g_explicit_and_multi_interval_timing_parsed_exactly() -> None:
    observation = _parse(
        _table(
            [
                ("2026-02-01", "NSE Equity", "Open", "09:15-15:30"),
                ("2026-11-08", "NSE Equity", "Open", "09:15-10:00, 11:30-12:30"),
            ]
        )
    )
    single = observation.dates[0]
    assert tuple((iv.start, iv.end) for iv in single.intervals) == ((time(9, 15), time(15, 30)),)
    multi = observation.dates[1]
    assert tuple((iv.start, iv.end) for iv in multi.intervals) == (
        (time(9, 15), time(10, 0)),
        (time(11, 30), time(12, 30)),
    )  # two disjoint blocks preserved, not collapsed to 09:15-12:30


# --------------------------------------------------------------------------- #
# H–J: fail-closed
# --------------------------------------------------------------------------- #
def test_h_malformed_date_fails_closed() -> None:
    observation = _parse(_table([("31-01-2026", "NSE Equity", "Closed", "")]))
    assert observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE
    assert observation.dates == ()


def test_i_missing_required_headers_fails_closed() -> None:
    observation = _parse(_table([("2026-01-26", "NSE", "")], headers=("Date", "Segment", "Foo")))
    assert observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE


def test_i_missing_table_fails_closed() -> None:
    observation = _parse("<html><body><p>no calendar here</p></body></html>")
    assert observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE


def test_j_duplicate_conflicting_rows_fail_closed() -> None:
    observation = _parse(
        _table(
            [("2026-01-26", "NSE Equity", "Closed", ""), ("2026-01-26", "NSE Equity", "Open", "")]
        )
    )
    assert observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE


def test_ambiguous_segment_fails_closed() -> None:
    observation = _parse(_table([("2026-01-26", "Mystery Board", "Closed", "")]))
    assert observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE


def test_malformed_timing_fails_closed() -> None:
    observation = _parse(_table([("2026-11-08", "NSE Equity", "Open", "09:15 to noon")]))
    assert observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE
