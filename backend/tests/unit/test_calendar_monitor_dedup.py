"""Discrepancy signature and re-alert dedup for the calendar monitor (ADR-011).

Proves the signature is deterministic (identical discrepancy → identical bounded
signature, so no unbounded growth or spurious re-alert), that a changed discrepancy
changes it, and that resolving to MATCH updates state without mutating the dataset.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from app.adapters.dhan.calendar_monitor_parser import DhanMarketHolidayParser
from app.adapters.dhan.calendar_monitor_source import DhanMarketHolidaySource
from app.market_engine.calendar_data import load_nse_cm_2026_dataset
from app.market_engine.clock import ManualClock
from app.services.calendar_monitor import CalendarComparisonStatus, CalendarMonitorService

_REF = datetime(2026, 8, 16, 2, 30, tzinfo=UTC)


def _html(rows: str) -> str:
    return (
        "<html><body><table>"
        "<tr><th>Date</th><th>Segment</th><th>Status</th></tr>"
        f"{rows}</table></body></html>"
    )


_DISCREPANCY_A = _html("<tr><td>2026-06-25</td><td>NSE Equity</td><td>Closed</td></tr>")
_DISCREPANCY_B = _html("<tr><td>2026-06-24</td><td>NSE Equity</td><td>Closed</td></tr>")
_MATCH_HTML = _html("<tr><td>2026-01-26</td><td>NSE Equity</td><td>Closed</td></tr>")


class _Sequenced:
    def __init__(self, htmls: list[str]) -> None:
        self._htmls = htmls
        self._index = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        html = self._htmls[min(self._index, len(self._htmls) - 1)]
        self._index += 1
        return httpx.Response(200, text=html)


def _service(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[CalendarMonitorService, object]:
    dataset = load_nse_cm_2026_dataset()
    source = DhanMarketHolidaySource(timeout_seconds=5.0, transport=httpx.MockTransport(handler))
    service = CalendarMonitorService(
        source=source, parser=DhanMarketHolidayParser(), dataset=dataset, clock=ManualClock(_REF)
    )
    return service, dataset


async def test_identical_discrepancy_yields_identical_signature() -> None:
    service, _dataset = _service(_Sequenced([_DISCREPANCY_A, _DISCREPANCY_A]))
    first = await service.check(reference=_REF)
    second = await service.check(reference=_REF)
    assert first.status is CalendarComparisonStatus.DHAN_NEW_CLOSED_DATE
    assert first.signature is not None
    assert second.signature == first.signature  # deterministic; no unbounded growth


async def test_changed_discrepancy_changes_signature() -> None:
    service, _dataset = _service(_Sequenced([_DISCREPANCY_A, _DISCREPANCY_B]))
    first = await service.check(reference=_REF)
    second = await service.check(reference=_REF)
    assert second.signature != first.signature


async def test_match_after_discrepancy_resolves_without_mutation() -> None:
    service, dataset = _service(_Sequenced([_DISCREPANCY_A, _MATCH_HTML]))
    before = dataset.model_dump()
    await service.check(reference=_REF)
    resolved = await service.check(reference=_REF)
    assert resolved.status is CalendarComparisonStatus.MATCH
    assert resolved.signature == "match"
    assert dataset.model_dump() == before
