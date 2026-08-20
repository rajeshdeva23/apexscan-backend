"""Fail-fast validation of malformed calendar datasets (ADR-011 addendum M13/MI4).

Each fixture starts from a synthetic, valid dataset and mutates exactly one property so
the failure is unambiguous. Malformed data must never be silently repaired, sorted, or
deduplicated — every case raises at construction. Synthetic dates only; the real 2026
data is asserted elsewhere.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.market_engine.calendar_data import TradingCalendarDataset


def _base() -> dict[str, Any]:
    """Return a fresh, valid synthetic dataset dictionary."""
    return {
        "dataset_id": "syn_cm",
        "version": "0.0.1",
        "segment": "TEST",
        "coverage_start": "2026-01-01",
        "coverage_end": "2026-12-31",
        "closed_dates": ["2026-06-01"],
        "open_sessions": ["2026-06-06"],
        "session_overrides": [
            {"trading_date": "2026-06-06", "intervals": [{"start": "09:15", "end": "15:30"}]}
        ],
        "provenance": [
            {
                "circular_id": "SYN/1",
                "circular_date": "2026-01-01",
                "segment": "TEST",
                "fact": "closed 2026-06-01; exceptional open 2026-06-06 with 09:15-15:30 interval",
            }
        ],
    }


def _load(payload: dict[str, Any]) -> TradingCalendarDataset:
    return TradingCalendarDataset.model_validate_json(json.dumps(payload))


def test_synthetic_base_is_valid() -> None:
    dataset = _load(_base())
    assert dataset.dataset_id == "syn_cm"


def test_inverted_coverage_is_rejected() -> None:
    payload = _base()
    payload["coverage_start"], payload["coverage_end"] = "2026-12-31", "2026-01-01"
    with pytest.raises(ValidationError):
        _load(payload)


def test_closed_date_outside_coverage_is_rejected() -> None:
    payload = _base()
    payload["closed_dates"].append("2027-06-01")
    with pytest.raises(ValidationError, match="outside coverage"):
        _load(payload)


def test_open_date_outside_coverage_is_rejected() -> None:
    payload = _base()
    payload["open_sessions"].append("2027-06-06")
    with pytest.raises(ValidationError, match="outside coverage"):
        _load(payload)


def test_override_date_outside_coverage_is_rejected() -> None:
    payload = _base()
    payload["open_sessions"].append("2027-06-06")
    payload["session_overrides"].append(
        {"trading_date": "2027-06-06", "intervals": [{"start": "09:15", "end": "15:30"}]}
    )
    with pytest.raises(ValidationError, match="outside coverage"):
        _load(payload)


def test_override_date_not_in_open_sessions_is_rejected() -> None:
    payload = _base()
    payload["session_overrides"][0]["trading_date"] = "2026-06-07"
    with pytest.raises(ValidationError, match="not a declared open session"):
        _load(payload)


def test_open_and_closed_conflict_is_rejected() -> None:
    payload = _base()
    payload["closed_dates"].append("2026-06-06")
    with pytest.raises(ValidationError, match="both open and closed"):
        _load(payload)


def test_duplicate_closed_date_is_rejected() -> None:
    payload = _base()
    payload["closed_dates"].append("2026-06-01")
    with pytest.raises(ValidationError, match="duplicate closed dates"):
        _load(payload)


def test_duplicate_override_date_is_rejected() -> None:
    payload = _base()
    payload["session_overrides"].append(copy.deepcopy(payload["session_overrides"][0]))
    with pytest.raises(ValidationError, match="duplicate session override dates"):
        _load(payload)


def test_invalid_interval_start_not_before_end_is_rejected() -> None:
    payload = _base()
    payload["session_overrides"][0]["intervals"] = [{"start": "15:30", "end": "09:15"}]
    with pytest.raises(ValidationError, match="start < end"):
        _load(payload)


def test_overlapping_intervals_are_rejected() -> None:
    payload = _base()
    payload["session_overrides"][0]["intervals"] = [
        {"start": "09:15", "end": "11:00"},
        {"start": "10:30", "end": "12:00"},
    ]
    with pytest.raises(ValidationError, match="strictly positive gap"):
        _load(payload)


def test_touching_intervals_are_rejected() -> None:
    payload = _base()
    payload["session_overrides"][0]["intervals"] = [
        {"start": "09:15", "end": "11:00"},
        {"start": "11:00", "end": "12:00"},
    ]
    with pytest.raises(ValidationError, match="strictly positive gap"):
        _load(payload)


def test_empty_provenance_is_rejected() -> None:
    payload = _base()
    payload["provenance"] = []
    with pytest.raises(ValidationError, match="at least one provenance"):
        _load(payload)


def test_missing_provenance_attestation_is_rejected() -> None:
    payload = _base()
    payload["provenance"][0]["fact"] = "closed 2026-06-01 only"
    with pytest.raises(ValidationError, match="missing provenance attestation"):
        _load(payload)


def test_blank_version_is_rejected() -> None:
    payload = _base()
    payload["version"] = "   "
    with pytest.raises(ValidationError, match="non-empty"):
        _load(payload)
