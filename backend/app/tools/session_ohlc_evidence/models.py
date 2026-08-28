"""Immutable data models for the current-session OHLC authority evidence collector.

Diagnostic artifacts only (DEPLOY-10 R4; ADR-008 tick-aggregate evidence procedure) —
these are never domain facts, never an authority source, and never mutate any runtime
state. Prices are exact ``Decimal``; models serialise deterministically to JSON.

Evidence is stored as raw observed values (not asserted conclusions): late-start and
reconnect continuity are *derived* by the evaluator from the recorded pre/post values, so
a verdict is reproducible and auditable from the artifact alone. Coverage is identity-based
(the expected instrument identities are recorded, not just a count), and the WS-vs-oracle
comparison records the exact ``tick_size`` used (``None`` = unknown → drift not permitted).
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
    DRIFT = "drift"  # differ but within an explicit, recorded one-tick allowance
    INDETERMINATE = "indeterminate"  # differ but tick size unknown → cannot classify (fail safe)
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
    trading_date: date | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None


class OracleComparison(_Frozen):
    """One WS-vs-REST(oracle) comparison for a single field in a single window.

    ``tick_size`` records the exact drift allowance used (``None`` = unknown; drift is then
    not permitted and a non-exact high/low difference is INDETERMINATE, not DRIFT).
    """

    window: str
    field: str  # "open" | "high" | "low"
    ws_value: Decimal | None
    rest_value: Decimal | None
    tick_size: Decimal | None
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

    @property
    def identity(self) -> str:
        """Canonical identity key ``EXCHANGE:SYMBOL`` used for coverage checks."""
        return f"{self.exchange}:{self.symbol}"


class LateStartEvidence(_Frozen):
    """Raw late-start evidence: extrema known before subscription vs the first post-sub obs.

    The evaluator derives whether the first post-subscription observation still contains the
    prior open/high/low; the booleans are never trusted as inputs.
    """

    observed: bool
    prior_observed_at: datetime | None = None
    prior_open: Decimal | None = None
    prior_high: Decimal | None = None
    prior_low: Decimal | None = None
    first_observed_at: datetime | None = None
    first_open: Decimal | None = None
    first_high: Decimal | None = None
    first_low: Decimal | None = None
    detail: str = ""


class ReconnectEvidence(_Frozen):
    """Raw pre/post-reconnect observations; continuity is derived by the evaluator (CSOA16)."""

    observed: bool
    pre: OhlcObservation | None = None
    post: OhlcObservation | None = None
    detail: str = ""


class EvidenceRecord(_Frozen):
    """The complete immutable evidence record for one collection run."""

    schema_version: str = "2.0.0"
    collector_version: str
    source_sha: str
    provider: str
    oracle_source: str = "dhan_rest_marketfeed_ohlc"
    oracle_independent: bool = False  # both WS and REST are Dhan-derived (not external truth)
    trading_date: date
    session_identity: str
    collection_start: datetime
    collection_end: datetime
    expected_instruments: tuple[str, ...]  # identity keys "EXCHANGE:SYMBOL"
    required_windows: tuple[str, ...] = ("early", "mid", "late")
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
    high_low_indeterminate: int = 0
    high_low_mismatch: int = 0
