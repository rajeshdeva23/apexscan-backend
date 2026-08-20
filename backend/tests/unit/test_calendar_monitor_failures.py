"""Fetch/parse failure handling and governance safety for the calendar monitor (ADR-011).

Uses the real Dhan source over an injected ``httpx.MockTransport`` (no network) so the
error mapping and the service's fail-closed state are exercised end to end. Proves fetch
and parse failures never mutate the dataset and never fall back to a settings calendar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.adapters.dhan.calendar_monitor_parser import DhanMarketHolidayParser
from app.adapters.dhan.calendar_monitor_source import DhanMarketHolidaySource
from app.market_engine.calendar_data import load_nse_cm_2026_dataset
from app.market_engine.clock import ManualClock
from app.services import calendar_monitor as monitor_module
from app.services.calendar_monitor import (
    CalendarComparisonStatus,
    CalendarMonitorParseStatus,
    CalendarMonitorService,
)

_REF = datetime(2026, 8, 16, 2, 30, tzinfo=UTC)

_MATCHING_HTML = (
    "<html><body><table>"
    "<tr><th>Date</th><th>Segment</th><th>Status</th></tr>"
    "<tr><td>2026-01-26</td><td>NSE Equity</td><td>Closed</td></tr>"
    "</table></body></html>"
)
_MALFORMED_HTML = "<html><body><p>no calendar table here</p></body></html>"


class _Sequenced:
    """A MockTransport handler stepping through per-call behaviours (last one repeats)."""

    def __init__(self, behaviours: list[Callable[[], httpx.Response]]) -> None:
        self._behaviours = behaviours
        self._index = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        behaviour = self._behaviours[min(self._index, len(self._behaviours) - 1)]
        self._index += 1
        return behaviour()


def _service(handler: Callable[[httpx.Request], httpx.Response], *, dataset_none: bool = False):
    source = DhanMarketHolidaySource(timeout_seconds=5.0, transport=httpx.MockTransport(handler))
    dataset = None if dataset_none else load_nse_cm_2026_dataset()
    return CalendarMonitorService(
        source=source, parser=DhanMarketHolidayParser(), dataset=dataset, clock=ManualClock(_REF)
    )


def _raise(exc: Exception) -> Callable[[], httpx.Response]:
    def behaviour() -> httpx.Response:
        raise exc

    return behaviour


def _ok(html: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, text=html)


# --------------------------------------------------------------------------- #
# U–Z
# --------------------------------------------------------------------------- #
async def test_u_timeout_becomes_fetch_failure() -> None:
    service = _service(_Sequenced([_raise(httpx.TimeoutException("slow"))]))
    state = await service.check(reference=_REF)
    assert state.status is CalendarComparisonStatus.DHAN_FETCH_FAILURE
    assert state.last_attempt_at == _REF


async def test_v_connection_error_becomes_fetch_failure() -> None:
    service = _service(_Sequenced([_raise(httpx.ConnectError("down"))]))
    state = await service.check(reference=_REF)
    assert state.status is CalendarComparisonStatus.DHAN_FETCH_FAILURE


async def test_w_malformed_html_becomes_parse_failure() -> None:
    service = _service(_Sequenced([_ok(_MALFORMED_HTML)]))
    state = await service.check(reference=_REF)
    assert state.status is CalendarComparisonStatus.DHAN_PARSE_FAILURE
    assert state.parse_status is CalendarMonitorParseStatus.PARSE_FAILURE


async def test_x_failure_does_not_mutate_dataset() -> None:
    dataset = load_nse_cm_2026_dataset()
    before = dataset.model_dump()
    source = DhanMarketHolidaySource(
        timeout_seconds=5.0,
        transport=httpx.MockTransport(_Sequenced([_raise(httpx.ConnectError("down"))])),
    )
    service = CalendarMonitorService(
        source=source, parser=DhanMarketHolidayParser(), dataset=dataset, clock=ManualClock(_REF)
    )
    await service.check(reference=_REF)
    assert dataset.model_dump() == before


async def test_y_recovery_after_failure_returns_to_match() -> None:
    service = _service(_Sequenced([_raise(httpx.ConnectError("down")), _ok(_MATCHING_HTML)]))
    first = await service.check(reference=_REF)
    assert first.status is CalendarComparisonStatus.DHAN_FETCH_FAILURE
    second = await service.check(reference=_REF)
    assert second.status is CalendarComparisonStatus.MATCH


async def test_z_missing_dataset_never_falls_back_to_settings() -> None:
    service = _service(_Sequenced([_ok(_MATCHING_HTML)]), dataset_none=True)
    state = await service.check(reference=_REF)
    assert state.status is CalendarComparisonStatus.AUTHORITATIVE_COVERAGE_MISSING


def test_z_monitor_code_reads_no_nse_holidays() -> None:
    adapters = Path(monitor_module.__file__).parent.parent / "adapters" / "dhan"
    for module_file in (
        Path(monitor_module.__file__),
        adapters / "calendar_monitor_parser.py",
        adapters / "calendar_monitor_source.py",
    ):
        assert "nse_holidays" not in module_file.read_text()
