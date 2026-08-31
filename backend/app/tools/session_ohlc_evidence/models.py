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

    MATCH = "match"  # identical Decimal values
    PROTOCOL_EQUIVALENT = "protocol_equivalent"  # identical IEEE-754 float32 wire representation
    DRIFT = "drift"  # differ but within an explicit, recorded one-tick allowance
    INDETERMINATE = "indeterminate"  # differ but tick size unknown → cannot classify (fail safe)
    MISMATCH = "mismatch"


class VerdictOutcome(StrEnum):
    """Deterministic evidence verdict (ADR-008 acceptance/rejection/inconclusive)."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class LateStartKind(StrEnum):
    """How late-start evidence was obtained (ADR-015 D7).

    ``provider_late_subscription`` (ADR-008 §A6) is a *new* provider subscription mid-session
    re-sending session-to-date extrema. ``observer_late_attach`` is an in-process evidence
    observer attaching to an already-live subscription: it proves the observer sees pre-attach
    extrema, but NOT that the provider re-sends them on a fresh subscription — so it does not by
    itself satisfy the ADR-008 provider late-start criterion.
    """

    PROVIDER_LATE_SUBSCRIPTION = "provider_late_subscription"
    OBSERVER_LATE_ATTACH = "observer_late_attach"


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
    method: str = "exact"  # exact | float32 | tick | unknown_tick | missing
    ws_float32_bits: str | None = None  # IEEE-754 float32 hex of ws_value (audit)
    rest_float32_bits: str | None = None  # IEEE-754 float32 hex of rest_value (audit)


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
    kind: LateStartKind = LateStartKind.PROVIDER_LATE_SUBSCRIPTION
    observer_attach_timestamp: datetime | None = None
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

    schema_version: str = "2.2.0"
    collector_version: str
    source_sha: str
    production_image: str | None = None  # running image tag when runtime-derivable (ADR-015 D10)
    provider: str
    oracle_source: str = "dhan_rest_marketfeed_ohlc"
    oracle_independent: bool = False  # both WS and REST are Dhan-derived (not external truth)
    trading_date: date
    session_identity: str
    collection_start: datetime
    collection_end: datetime
    expected_instruments: tuple[str, ...]  # identity keys "EXCHANGE:SYMBOL"
    pending_instruments: tuple[str, ...] = ()  # expected identities with no WS observation
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
    protocol_equivalent: int = 0  # comparisons that matched only by float32 wire representation
    high_low_drift: int = 0
    high_low_indeterminate: int = 0
    high_low_mismatch: int = 0
