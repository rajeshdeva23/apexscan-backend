"""Broker-neutral enumerations for the Strategy contract layer (P5.1).

Names follow the frozen governance verbatim: strategy categories from docs/07 §8,
evaluation statuses from docs/07 §12.1, and lifecycle states / emission policies
from ADR-007 (D1 / D10). Triggers and candle-completeness needs follow the Phase-5
design and ADR-006. These are pure contract vocabulary — no behaviour, no routing.
"""

from __future__ import annotations

from enum import StrEnum


class StrategyCategory(StrEnum):
    """Conceptual family a strategy belongs to (docs/07 §8; labels, not logic)."""

    MOMENTUM = "momentum"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    TREND = "trend"
    RANGE = "range"
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    OPENING_SESSION = "opening_session"
    VOLUME = "volume"
    MARKET_STRUCTURE = "market_structure"


class StrategyLifecycleState(StrEnum):
    """Runtime lifecycle states owned by the Strategy Manager (ADR-007 D1).

    Defined here as a pure contract vocabulary; the transition rules (ADR-007 D2)
    are implemented in P5.2, not here.
    """

    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"
    SHUTDOWN = "shutdown"


class EvaluationStatus(StrEnum):
    """The outcome of one strategy evaluation (docs/07 §12.1)."""

    MATCHED = "matched"
    NO_MATCH = "no_match"
    SKIPPED = "skipped"
    ERROR = "error"


class StrategyTrigger(StrEnum):
    """The market event that makes a strategy eligible for evaluation (ADR-007 D10)."""

    ON_CONTEXT = "on_context"
    ON_TICK = "on_tick"
    ON_QUOTE = "on_quote"
    ON_CANDLE_FINALIZED = "on_candle_finalized"
    ON_SESSION_TRANSITION = "on_session_transition"
    ON_HISTORICAL_READY = "on_historical_ready"


class CandleCompleteness(StrEnum):
    """Which candle facts a strategy is permitted to consume (ADR-006).

    ``AUTHORITATIVE_ONLY`` restricts a strategy to canonical finalized candles;
    ``PARTIAL_ALLOWED`` additionally permits inspecting in-progress/incomplete
    candle facts when the strategy's own specification explicitly allows it. Neither
    treats an incomplete candle as authoritative.
    """

    AUTHORITATIVE_ONLY = "authoritative_only"
    PARTIAL_ALLOWED = "partial_allowed"


class FactNeed(StrEnum):
    """A MarketContext fact a strategy declares it requires to evaluate."""

    LATEST_TICK = "latest_tick"
    LATEST_QUOTE = "latest_quote"
    SESSION = "session"
    PREVIOUS_SESSION = "previous_session"
    SESSION_STATISTICS = "session_statistics"


class EmissionPolicy(StrEnum):
    """How a strategy's results are emitted downstream (ADR-007 D10)."""

    CONTINUOUS = "continuous"
    EDGE_TRIGGERED = "edge_triggered"
    ONE_SHOT_PER_SESSION = "one_shot_per_session"
