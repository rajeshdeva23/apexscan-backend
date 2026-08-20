"""Deterministic, offline loader for the packaged NSE 2026 CM calendar dataset.

The loader reads the version-controlled ``nse_cm_2026.json`` from this package via
:mod:`importlib.resources`, validates it into a :class:`TradingCalendarDataset`, and
returns it. It performs no network I/O, reads no wall clock (``date.today``/``now``),
and knows nothing about any broker or provider — the same packaged bytes always yield
an equal dataset (ADR-011 addendum MI19). Malformed data fails fast at validation and
is never silently repaired.
"""

from __future__ import annotations

from importlib.resources import files

from app.market_engine.calendar_data.dataset import TradingCalendarDataset

_PACKAGE = "app.market_engine.calendar_data"
_RESOURCE = "nse_cm_2026.json"


def load_nse_cm_2026_dataset() -> TradingCalendarDataset:
    """Load and validate the packaged NSE 2026 Capital-Market calendar dataset.

    Returns:
        The validated :class:`TradingCalendarDataset` for the 2026 NSE cash-equity
        (Capital Market) segment.

    Raises:
        pydantic.ValidationError: If the packaged JSON is malformed or violates any
            dataset-level rule (fail-closed; never silently repaired).
    """
    raw = files(_PACKAGE).joinpath(_RESOURCE).read_text(encoding="utf-8")
    return TradingCalendarDataset.model_validate_json(raw)
