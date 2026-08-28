"""Immutable data models for the current-session OHLC authority evidence collector.

Diagnostic artifacts only (DEPLOY-10 R4; ADR-008 tick-aggregate evidence procedure) —
these are never domain facts, never an authority source, and never mutate any runtime
state. Prices are exact ``Decimal``; models serialise deterministically to JSON.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    """Immutable base for evidence records."""

    model_config = ConfigDict(frozen=True)


class Classification(StrEnum):
    """Result of one WS-vs-oracle price comparison."""

    MATCH = "match"
    DRIFT = "drift"  # differ but within the governed one-tick temporal-drift allowance
    MISMATCH = "mismatch"


class VerdictOutcome(StrEnum):
    """Deterministic evidence verdict (ADR-008 acceptance/rejection/inconclusive)."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class OhlcObservation(_Frozen):
    """One open/high/low observation from a single source at one instant."""

    source: str  # "ws" | "rest"
    window: str  # "early" | "mid" | "late" | free-form label
    observed_at: datetime
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None


class OracleComparison(_Frozen):
    """One WS-vs-REST(oracle) comparison for a single field in a single window."""

    window: str
    field: str  # "open" | "high" | "low"
    ws_value: Decimal | None
    rest_value: Decimal | None
    classification: Classification


class MonotonicityResult(_Frozen):
    """Within-session monotonicity outcome for one instrument."""

    open_stable: bool
    high_non_decreasing: bool
    low_non_increasing: bool
    violations: tuple[str, ...] = ()


class InstrumentEvidence(_Frozen):
    """All evidence collected for one instrument during one trading session."""

    exchange: str
    symbol: str
    security_id: str
    trading_date: date
    ws_observations: tuple[OhlcObservation, ...] = ()
    rest_observations: tuple[OhlcObservation, ...] = ()
    oracle_comparisons: tuple[OracleComparison, ...] = ()
    monotonicity: MonotonicityResult | None = None


class LateStartEvidence(_Frozen):
    """Evidence that the first observation after a late start still carries prior extrema."""

    observed: bool
    first_observed_at: datetime | None = None
    open_present: bool | None = None
    contains_prior_high: bool | None = None
    contains_prior_low: bool | None = None
    detail: str = ""


class ReconnectEvidence(_Frozen):
    """Pre/post-reconnect session-to-date continuity evidence (CSOA16)."""

    observed: bool
    pre: OhlcObservation | None = None
    post: OhlcObservation | None = None
    same_session: bool | None = None
    open_preserved: bool | None = None
    high_preserved: bool | None = None
    low_preserved: bool | None = None
    detail: str = ""


class EvidenceRecord(_Frozen):
    """The complete immutable evidence record for one collection run."""

    schema_version: str = "1.0.0"
    collector_version: str
    source_sha: str
    provider: str
    trading_date: date
    session_identity: str
    collection_start: datetime
    collection_end: datetime
    universe_expected: int
    universe_observed: int
    sample_windows: tuple[str, ...]
    instruments: tuple[InstrumentEvidence, ...]
    late_start: LateStartEvidence | None = None
    reconnect: ReconnectEvidence | None = None
    csoa16_required: bool = True
    oracle_available: bool = True


class Verdict(_Frozen):
    """The deterministic evaluation outcome and its structured reasons."""

    outcome: VerdictOutcome
    reasons: tuple[str, ...]
    open_mismatches: int = 0
    monotonicity_violations: int = 0
    high_low_drift: int = 0
    high_low_mismatch: int = 0
