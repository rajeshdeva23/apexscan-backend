"""Pure HTML-to-observation parser for the secondary Dhan calendar monitor (ADR-011).

This module is OBSERVATION-ONLY, provider-secondary evidence. It never mutates any
authoritative calendar structure and never fetches the network: it turns a page of
already-retrieved HTML into a broker-neutral :class:`DhanCalendarObservation` whose
fields carry only dates, statuses, and intervals. Any discrepancy the downstream
comparison surfaces is a *possible calendar discrepancy requiring authoritative NSE
review*, never an automatic calendar update (ADR-011 governance).

Expected HTML table structure
------------------------------
Because the live ``dhan.co`` page must never be fetched during development or CI, this
parser recognises a CLEAR, DOCUMENTED table shape and fails closed
(``parse_status=PARSE_FAILURE``) against anything it cannot confidently read. The
recogniser expects at least one ``<table>`` whose header row (a row containing ``<th>``
cells, or otherwise the first row) names these columns (case-insensitive; extra columns
are ignored):

* ``Date`` (required) — an ISO ``YYYY-MM-DD`` date. Any other format is malformed and
  fails the whole parse.
* ``Segment`` / ``Exchange`` (required) — the exchange-segment indicator used to filter
  to NSE cash equity (see below).
* ``Status`` / ``Type`` (required) — ``Closed``/``Holiday`` (a non-trading day) or
  ``Open``/``Muhurat``/``Special`` (an exceptional OPEN session).
* ``Timings`` (optional) — for OPEN rows only, one or more ``HH:MM-HH:MM`` intervals
  separated by commas or semicolons (e.g. ``09:15-15:30`` or ``09:15-10:00, 11:30-12:30``).
  Multiple disjoint intervals are preserved in order and never collapsed. Absent/empty
  timing yields ``intervals=()`` — timing is never fabricated.

Segment filtering (NSE cash equity only)
-----------------------------------------
* A row whose segment is confidently NSE cash equity is kept.
* A row whose segment is confidently a different segment (MCX, currency/CDS, clearing,
  BSE, commodity) is ignored.
* A row whose segment is genuinely ambiguous — neither confidently NSE equity nor
  confidently something else — fails the whole parse rather than being guessed.

Fail-closed rules
-----------------
Missing table/headers, a malformed date, an unrecognisable status, a malformed timing,
an ambiguous segment, or duplicate/conflicting rows for the same date all yield
``PARSE_FAILURE`` with ``dates=()``. The parser is deterministic: no clock, no network,
no dataset access, no mutation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, model_validator

_TIME_FORMAT = "%H:%M"
_INTERVAL_SPLIT = re.compile(r"[;,]")
_INTERVAL_PATTERN = re.compile(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")

_DATE_HEADERS = frozenset({"date", "trading date", "holiday date"})
_SEGMENT_HEADERS = frozenset(
    {"segment", "exchange", "exchange segment", "exchange/segment", "market"}
)
_STATUS_HEADERS = frozenset({"status", "type"})
_TIMING_HEADERS = frozenset({"timings", "timing", "session timings", "hours", "session hours"})

_NSE_EQUITY_SEGMENTS = frozenset(
    {"nse", "nse eq", "nse_eq", "nse equity", "nse cm", "nse cash", "capital market", "equity"}
)
_NSE_EQUITY_TOKENS = ("equity", "cash", " eq", "capital market")
_NON_NSE_TOKENS = ("mcx", "currency", "cds", "clearing", "bse", "commodity")

_CLOSED_STATUSES = frozenset({"closed", "close", "holiday", "trading holiday"})
_OPEN_STATUSES = frozenset(
    {"open", "muhurat", "muhurat trading", "special", "special session", "live", "live session"}
)


class CalendarObservationParseStatus(StrEnum):
    """Whether the HTML was confidently parsed into a calendar observation."""

    OK = "ok"
    PARSE_FAILURE = "parse_failure"


class ObservedDateStatus(StrEnum):
    """The date-level status a parsed calendar row reports for the exchange."""

    CLOSED = "closed"
    OPEN = "open"


class ObservedInterval(BaseModel):
    """One exchange-local ``[start, end)`` interval observed on the secondary page.

    Attributes:
        start: The exchange-local interval start.
        end: The exchange-local interval end.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start: time
    end: time

    @model_validator(mode="after")
    def _validate_bounds(self) -> ObservedInterval:
        """Reject a non-increasing observed interval, failing fast at construction."""
        if self.start >= self.end:
            raise ValueError("observed interval requires start < end")
        return self


class ObservedCalendarDate(BaseModel):
    """One date's observed status and (for OPEN dates that stated timing) its intervals.

    Attributes:
        observed_date: The exchange-local date the row concerns.
        status: The observed date-level status (CLOSED or OPEN).
        intervals: The observed live intervals; non-empty only for OPEN dates whose row
            explicitly stated timing. Never fabricated for CLOSED dates or for OPEN dates
            with no stated timing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    observed_date: date
    status: ObservedDateStatus
    intervals: tuple[ObservedInterval, ...] = ()


class DhanCalendarObservation(BaseModel):
    """A broker-neutral observation of one secondary calendar page.

    The fields are provider-neutral (dates, statuses, intervals only); ``source`` records
    where the HTML came from for operational traceability. This model is consumed by the
    services layer (services importing an adapter type is the correct direction).

    Attributes:
        source: An opaque label identifying where the parsed HTML originated.
        source_year: The single calendar year the observed dates share, or ``None`` when
            the observation spans multiple years or is empty.
        parse_status: Whether the page was confidently parsed.
        dates: The observed calendar dates (empty on a parse failure).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str
    source_year: int | None
    parse_status: CalendarObservationParseStatus
    dates: tuple[ObservedCalendarDate, ...]


class _ParseFailureError(Exception):
    """Internal signal that the page cannot be confidently parsed (fail closed)."""


@dataclass(frozen=True, slots=True)
class _Columns:
    """Resolved column indices for the recognised holiday table."""

    date: int
    segment: int
    status: int
    timings: int | None


class _HolidayTableParser(HTMLParser):
    """Collect every table as an ordered list of ``(is_header_row, cell_texts)`` rows."""

    def __init__(self) -> None:
        """Initialise the collector with no open table, row, or cell."""
        super().__init__(convert_charrefs=True)
        self.tables: list[list[tuple[bool, list[str]]]] = []
        self._table: list[tuple[bool, list[str]]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._row_is_header = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a table, row, or cell as the corresponding start tag is encountered."""
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
            self._row_is_header = False
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th":
                self._row_is_header = True

    def handle_endtag(self, tag: str) -> None:
        """Close the open cell, row, or table as the corresponding end tag is encountered."""
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append((self._row_is_header, self._row))
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        """Accumulate text into the open cell, if any."""
        if self._cell is not None:
            self._cell.append(data)


class DhanMarketHolidayParser:
    """Parse secondary Dhan market-holiday HTML into a broker-neutral observation."""

    def parse(self, html: str, *, source: str) -> DhanCalendarObservation:
        """Parse ``html`` into a :class:`DhanCalendarObservation` (pure, fail-closed).

        Args:
            html: The already-retrieved page HTML (never fetched here).
            source: An opaque label recording where the HTML originated.

        Returns:
            An OK observation with the recognised NSE cash-equity dates, or a
            ``PARSE_FAILURE`` observation with ``dates=()`` when the page cannot be
            confidently read.
        """
        collector = _HolidayTableParser()
        collector.feed(html)
        collector.close()
        try:
            dates = _extract_nse_equity_dates(collector.tables)
        except _ParseFailureError:
            return DhanCalendarObservation(
                source=source,
                source_year=None,
                parse_status=CalendarObservationParseStatus.PARSE_FAILURE,
                dates=(),
            )
        return DhanCalendarObservation(
            source=source,
            source_year=_single_year(dates),
            parse_status=CalendarObservationParseStatus.OK,
            dates=dates,
        )


def _extract_nse_equity_dates(
    tables: list[list[tuple[bool, list[str]]]],
) -> tuple[ObservedCalendarDate, ...]:
    """Find the recognised holiday table and reduce its rows to observed dates."""
    for table in tables:
        header_index, header = _find_header(table)
        if header is None:
            continue
        columns = _column_map(header)
        if columns is not None:
            data_rows = [row for index, (_h, row) in enumerate(table) if index != header_index]
            return _rows_to_dates(data_rows, columns)
    raise _ParseFailureError


def _find_header(table: list[tuple[bool, list[str]]]) -> tuple[int, list[str] | None]:
    """Return the header row's index and cells (the first ``<th>`` row, else the first row)."""
    for index, (is_header, cells) in enumerate(table):
        if is_header:
            return index, cells
    if table:
        return 0, table[0][1]
    return -1, None


def _column_map(header: list[str]) -> _Columns | None:
    """Map the required columns from a header row, or ``None`` when structure is missing."""
    normalized = [cell.strip().lower() for cell in header]
    date_i = _index_of(normalized, _DATE_HEADERS)
    segment_i = _index_of(normalized, _SEGMENT_HEADERS)
    status_i = _index_of(normalized, _STATUS_HEADERS)
    if date_i is None or segment_i is None or status_i is None:
        return None
    return _Columns(
        date=date_i,
        segment=segment_i,
        status=status_i,
        timings=_index_of(normalized, _TIMING_HEADERS),
    )


def _index_of(normalized: list[str], names: frozenset[str]) -> int | None:
    """Return the first index whose normalized header value is one of ``names``."""
    for index, value in enumerate(normalized):
        if value in names:
            return index
    return None


def _rows_to_dates(rows: list[list[str]], columns: _Columns) -> tuple[ObservedCalendarDate, ...]:
    """Reduce data rows to observed dates, ignoring non-NSE rows and rejecting duplicates."""
    observed: list[ObservedCalendarDate] = []
    seen: set[date] = set()
    for row in rows:
        entry = _row_to_observed(row, columns)
        if entry is None:
            continue
        if entry.observed_date in seen:
            raise _ParseFailureError
        seen.add(entry.observed_date)
        observed.append(entry)
    return tuple(observed)


def _row_to_observed(row: list[str], columns: _Columns) -> ObservedCalendarDate | None:
    """Reduce one row to an observed date, or ``None`` when it is confidently non-NSE."""
    try:
        segment = row[columns.segment]
        status_text = row[columns.status]
        date_text = row[columns.date]
    except IndexError as error:
        raise _ParseFailureError from error
    if _is_non_nse_equity_segment(segment):
        return None
    if not _is_nse_equity_segment(segment):
        raise _ParseFailureError
    status = _parse_status(status_text)
    return ObservedCalendarDate(
        observed_date=_parse_iso_date(date_text),
        status=status,
        intervals=_row_intervals(row, columns, status),
    )


def _row_intervals(
    row: list[str], columns: _Columns, status: ObservedDateStatus
) -> tuple[ObservedInterval, ...]:
    """Return the observed intervals for an OPEN row with a timings column, else ``()``."""
    if status is not ObservedDateStatus.OPEN or columns.timings is None:
        return ()
    try:
        text = row[columns.timings]
    except IndexError as error:
        raise _ParseFailureError from error
    return _parse_intervals(text)


def _is_nse_equity_segment(segment: str) -> bool:
    """Return whether a segment confidently denotes NSE cash equity."""
    normalized = segment.strip().lower()
    if normalized in _NSE_EQUITY_SEGMENTS:
        return True
    return "nse" in normalized and any(token in normalized for token in _NSE_EQUITY_TOKENS)


def _is_non_nse_equity_segment(segment: str) -> bool:
    """Return whether a segment confidently denotes a non-NSE-equity segment."""
    normalized = segment.strip().lower()
    if _is_nse_equity_segment(normalized):
        return False
    return any(token in normalized for token in _NON_NSE_TOKENS)


def _parse_status(text: str) -> ObservedDateStatus:
    """Map a status cell to CLOSED or OPEN, failing closed on anything unrecognised."""
    normalized = text.strip().lower()
    if normalized in _CLOSED_STATUSES:
        return ObservedDateStatus.CLOSED
    if normalized in _OPEN_STATUSES:
        return ObservedDateStatus.OPEN
    raise _ParseFailureError


def _parse_iso_date(text: str) -> date:
    """Parse a strict ISO ``YYYY-MM-DD`` date, failing closed on any other format."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError as error:
        raise _ParseFailureError from error


def _parse_intervals(text: str) -> tuple[ObservedInterval, ...]:
    """Parse ordered ``HH:MM-HH:MM`` intervals, preserving disjoint blocks (never merged)."""
    stripped = text.strip()
    if not stripped:
        return ()
    intervals: list[ObservedInterval] = []
    for part in _INTERVAL_SPLIT.split(stripped):
        candidate = part.strip()
        if not candidate:
            continue
        match = _INTERVAL_PATTERN.fullmatch(candidate)
        if match is None:
            raise _ParseFailureError
        intervals.append(_build_interval(match.group(1), match.group(2)))
    if not intervals:
        raise _ParseFailureError
    return tuple(intervals)


def _build_interval(start_text: str, end_text: str) -> ObservedInterval:
    """Build one validated observed interval, failing closed on invalid bounds."""
    try:
        return ObservedInterval(start=_parse_hhmm(start_text), end=_parse_hhmm(end_text))
    except ValueError as error:
        raise _ParseFailureError from error


def _parse_hhmm(value: str) -> time:
    """Parse an exchange-local ``HH:MM`` time, failing closed on an invalid value."""
    try:
        return datetime.strptime(value, _TIME_FORMAT).time()
    except ValueError as error:
        raise _ParseFailureError from error


def _single_year(dates: tuple[ObservedCalendarDate, ...]) -> int | None:
    """Return the single year all observed dates share, or ``None`` otherwise."""
    years = {entry.observed_date.year for entry in dates}
    if len(years) == 1:
        return next(iter(years))
    return None
