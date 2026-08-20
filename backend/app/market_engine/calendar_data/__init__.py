"""Governed, broker-neutral, version-controlled trading-calendar datasets (ADR-011-DATA-R1).

Public surface: the validated :class:`TradingCalendarDataset` model (with its
:class:`CalendarProvenance` sub-model) and the deterministic offline loader for the
packaged NSE 2026 Capital-Market dataset. This package holds calendar *data* and its
projection onto the Market-Engine domain types; it wires nothing into production
composition.
"""

from __future__ import annotations

from app.market_engine.calendar_data.dataset import (
    CalendarProvenance,
    IntervalSpec,
    SessionOverrideSpec,
    TradingCalendarDataset,
)
from app.market_engine.calendar_data.loader import load_nse_cm_2026_dataset

__all__ = [
    "CalendarProvenance",
    "IntervalSpec",
    "SessionOverrideSpec",
    "TradingCalendarDataset",
    "load_nse_cm_2026_dataset",
]
