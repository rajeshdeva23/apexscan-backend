"""Immutable declaration of what a strategy needs to evaluate (P5.1; docs/07 §16).

A strategy *declares* its data needs; it never fetches them (docs/07 §4.11). This
reuses the Phase-4 :class:`HistoricalRequirement` and :class:`Timeframe` contracts
verbatim — no duplicate historical/timeframe models — and adds the fact needs,
trigger, and candle-completeness policy. Collections are normalised to a canonical,
deduplicated order for determinism. The multi-strategy *union* is not computed here
(the Strategy Manager owns it, ADR-007 D8).
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field, model_validator

from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    timeframe_ordering_key,
)
from app.market_engine.timeframe import Timeframe
from app.strategies.enums import CandleCompleteness, FactNeed, StrategyTrigger
from app.strategies.models import FrozenModel

_INITIAL_CONTEXT_VERSION = 1


class FactFreshnessRequirement(FrozenModel):
    """A strategy's maximum acceptable age for a declared fact (ADR-009 D6).

    Freshness is consumer-specific and separate from a fact's provenance/authority:
    an ``AUTHORITATIVE`` fact older than ``max_age`` is not usable for this consumer.
    No global default duration exists; each strategy declares its own governed value.

    Attributes:
        fact: The declared fact this freshness bound applies to.
        max_age: The strictly-positive maximum acceptable age of the fact.
    """

    fact: FactNeed
    max_age: timedelta = Field(gt=timedelta(0))


class StrategyRequirements(FrozenModel):
    """Immutable, canonical declaration of a strategy's data/fact requirements.

    Attributes:
        historical: Required historical series (Phase-4 contract), deduplicated and
            canonically ordered; the manager unions these across strategies later.
        live_timeframes: Required live candle timeframes, deduplicated and ordered.
        fact_needs: MarketContext facts the strategy requires, deduplicated/ordered.
        trigger: The market event that makes the strategy eligible to evaluate.
        candle_completeness: Whether only authoritative candles may be consumed.
        min_context_version: Lowest MarketContext version the strategy accepts.
        freshness: Per-fact maximum-age requirements (ADR-009 D6), canonically ordered;
            each entry must reference a declared fact need, with no duplicate fact.
    """

    historical: tuple[HistoricalRequirement, ...] = ()
    live_timeframes: tuple[Timeframe, ...] = ()
    fact_needs: tuple[FactNeed, ...] = ()
    trigger: StrategyTrigger
    candle_completeness: CandleCompleteness
    min_context_version: int = Field(default=_INITIAL_CONTEXT_VERSION, ge=_INITIAL_CONTEXT_VERSION)
    freshness: tuple[FactFreshnessRequirement, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: object) -> object:
        """Deduplicate and canonically order the requirement collections."""
        if not isinstance(data, dict):
            return data
        updated = dict(data)
        updated["historical"] = _ordered_historical(data.get("historical"))
        updated["live_timeframes"] = _ordered_timeframes(data.get("live_timeframes"))
        updated["fact_needs"] = _ordered_fact_needs(data.get("fact_needs"))
        updated["freshness"] = _ordered_freshness(data.get("freshness"))
        return {key: value for key, value in updated.items() if value is not None}

    @model_validator(mode="after")
    def _validate_freshness(self) -> StrategyRequirements:
        facts = [entry.fact for entry in self.freshness]
        if len(set(facts)) != len(facts):
            raise ValueError("freshness must not define the same fact twice")
        needs = set(self.fact_needs)
        if any(entry.fact not in needs for entry in self.freshness):
            raise ValueError("freshness may only reference a declared fact need")
        return self


def _ordered_historical(value: object) -> object:
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, HistoricalRequirement) for item in value
    ):
        return value
    unique = tuple(dict.fromkeys(value))
    return tuple(
        sorted(unique, key=lambda req: (*timeframe_ordering_key(req.timeframe), req.lookback))
    )


def _ordered_timeframes(value: object) -> object:
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, Timeframe) for item in value
    ):
        return value
    unique = tuple(dict.fromkeys(value))
    return tuple(sorted(unique, key=timeframe_ordering_key))


def _ordered_fact_needs(value: object) -> object:
    if not isinstance(value, list | tuple) or not all(isinstance(item, FactNeed) for item in value):
        return value
    unique = tuple(dict.fromkeys(value))
    return tuple(sorted(unique, key=lambda need: need.value))


def _ordered_freshness(value: object) -> object:
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, FactFreshnessRequirement) for item in value
    ):
        return value
    return tuple(sorted(value, key=lambda entry: entry.fact.value))
