"""Test-only builders and configurable fake strategies for P5.3 manager tests.

These fakes exercise the StrategyManager's routing, triggers, readiness gating,
and failure isolation without any concrete strategy logic. No strategy names,
scores, or market rules are encoded here (those belong to later Phase-5 slices).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.market_engine.context import (
    Candle,
    MarketContext,
    MarketState,
    SessionContext,
    TimeframeCandles,
)
from app.market_engine.historical.context import (
    HistoricalContext,
    HistoricalSeries,
    PreviousSessionFacts,
)
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument, Quote, Tick
from app.strategies.configuration import StrategyConfiguration
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import (
    CandleCompleteness,
    EmissionPolicy,
    EvaluationStatus,
    FactNeed,
    StrategyCategory,
    StrategyTrigger,
)
from app.strategies.requirements import StrategyRequirements
from app.strategies.results import MetricEntry, StrategyEvaluation, StrategyResult
from app.strategy_manager.lifecycle import StrategyLifecycle

INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
OTHER_INSTRUMENT = Instrument(exchange="NSE", symbol="TCS")
EVENT_TIME = datetime(2026, 8, 9, 9, 15, tzinfo=UTC)
ONE_MINUTE = Timeframe.minutes(1)


class FakeConfig(StrategyConfiguration):
    """A concrete strategy configuration subtype used to test type-gated readiness."""


def make_tick(price: str) -> Tick:
    """Build a tick at ``price`` for the shared instrument."""
    return Tick(instrument=INSTRUMENT, event_timestamp=EVENT_TIME, last_price=Decimal(price))


def make_quote(bid: str, ask: str) -> Quote:
    """Build a quote with the given bid/ask for the shared instrument."""
    return Quote(
        instrument=INSTRUMENT,
        event_timestamp=EVENT_TIME,
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_quantity=1,
        ask_quantity=1,
    )


def make_candle(minute: int, *, instrument: Instrument = INSTRUMENT) -> Candle:
    """Build one 1-minute candle starting ``minute`` minutes after open."""
    start = EVENT_TIME + timedelta(minutes=minute)
    return Candle(
        instrument=instrument,
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=1),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100.5"),
        traded_quantity=10,
    )


def candle_set(*, finalized: tuple[Candle, ...]) -> TimeframeCandles:
    """Wrap finalized candles as a 1-minute :class:`TimeframeCandles`."""
    return TimeframeCandles(timeframe=ONE_MINUTE, finalized=finalized)


def make_session(state: MarketState) -> SessionContext:
    """Build a session context in the given market state."""
    return SessionContext(
        trading_date=date(2026, 8, 9), market_state=state, exchange_timezone="Asia/Kolkata"
    )


def make_historical(*, lookback: int = 1, with_previous: bool = False) -> HistoricalContext:
    """Build a historical context with ``lookback`` 1-minute candles."""
    candles = tuple(make_candle(-index - 1) for index in range(lookback))
    previous = (
        PreviousSessionFacts(trading_date=date(2026, 8, 8), candle=make_candle(-100))
        if with_previous
        else None
    )
    return HistoricalContext(
        instrument=INSTRUMENT,
        previous_session=previous,
        series=(HistoricalSeries(timeframe=ONE_MINUTE, candles=candles),),
    )


def historical_requirement(lookback: int = 1) -> HistoricalRequirement:
    """Return a 1-minute historical requirement for ``lookback`` candles."""
    return HistoricalRequirement(timeframe=ONE_MINUTE, lookback=lookback)


def make_context(
    *,
    version: int = 1,
    instrument: Instrument = INSTRUMENT,
    latest_tick: Tick | None = None,
    latest_quote: Quote | None = None,
    candle_sets: tuple[TimeframeCandles, ...] = (),
    session: SessionContext | None = None,
    historical: HistoricalContext | None = None,
) -> MarketContext:
    """Build a MarketContext at an explicit version with the given observable state."""
    return MarketContext(
        instrument=instrument,
        version=version,
        sequence=version,
        event_timestamp=EVENT_TIME,
        observed_at=EVENT_TIME,
        latest_tick=latest_tick,
        latest_quote=latest_quote,
        candle_sets=candle_sets,
        session=session,
        historical=historical,
    )


def requirements(
    *,
    trigger: StrategyTrigger = StrategyTrigger.ON_CONTEXT,
    completeness: CandleCompleteness = CandleCompleteness.AUTHORITATIVE_ONLY,
    historical: tuple[HistoricalRequirement, ...] = (),
    live_timeframes: tuple[Timeframe, ...] = (),
    fact_needs: tuple[FactNeed, ...] = (),
    min_context_version: int = 1,
) -> StrategyRequirements:
    """Build strategy requirements with sensible test defaults."""
    return StrategyRequirements(
        trigger=trigger,
        candle_completeness=completeness,
        historical=historical,
        live_timeframes=live_timeframes,
        fact_needs=fact_needs,
        min_context_version=min_context_version,
    )


def evaluation(
    context: MarketContext,
    *,
    status: EvaluationStatus = EvaluationStatus.MATCHED,
    score: str | None = None,
    confidence: str | None = None,
    reason_codes: tuple[str, ...] = ("MATCH",),
    metrics: tuple[MetricEntry, ...] = (),
) -> StrategyEvaluation:
    """Build a StrategyEvaluation for ``context`` with explicit typed outcome fields."""
    return StrategyEvaluation(
        instrument=context.instrument,
        context_version=context.version,
        status=status,
        score=Decimal(score) if score is not None else None,
        confidence=Decimal(confidence) if confidence is not None else None,
        reason_codes=reason_codes if status is EvaluationStatus.MATCHED else (),
        metrics=metrics,
    )


def result(
    *,
    strategy_id: str = "alpha",
    instrument: Instrument = INSTRUMENT,
    context_version: int = 1,
    status: EvaluationStatus = EvaluationStatus.MATCHED,
    score: str | None = "1",
    confidence: str | None = None,
    reason_codes: tuple[str, ...] = ("MATCH",),
    metrics: tuple[MetricEntry, ...] = (),
) -> StrategyResult:
    """Build an immutable StrategyResult for dedup/ranking unit tests."""
    return StrategyResult(
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        config_version="1.0.0",
        instrument=instrument,
        context_version=context_version,
        evaluation_timestamp=EVENT_TIME,
        status=status,
        score=Decimal(score) if score is not None else None,
        confidence=Decimal(confidence) if confidence is not None else None,
        reason_codes=reason_codes if status is EvaluationStatus.MATCHED else (),
        metrics=metrics,
    )


class FakeStrategy:
    """A configurable, contract-conforming strategy for routing/readiness tests."""

    def __init__(
        self,
        strategy_id: str,
        *,
        reqs: StrategyRequirements | None = None,
        configuration_type: type[StrategyConfiguration] = StrategyConfiguration,
        behavior: str = "no_match",
        emission_policy: EmissionPolicy = EmissionPolicy.CONTINUOUS,
        evaluator: Callable[[MarketContext], StrategyEvaluation] | None = None,
        min_context_version: int = 1,
        max_context_version: int | None = None,
    ) -> None:
        """Configure the fake's identity, requirements, and evaluation behavior."""
        self._id = strategy_id
        self._reqs = reqs if reqs is not None else requirements()
        self._configuration_type = configuration_type
        self._behavior = behavior
        self._emission_policy = emission_policy
        self._evaluator = evaluator
        self._min = min_context_version
        self._max = max_context_version
        self.calls: list[MarketContext] = []

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return the descriptor derived from the configured identity/versions."""
        return StrategyDescriptor(
            strategy_id=self._id,
            display_name=f"Fake {self._id}",
            description="A test strategy.",
            version="1.0.0",
            category=StrategyCategory.MOMENTUM,
            emission_policy=self._emission_policy,
            min_context_version=self._min,
            max_context_version=self._max,
        )

    @property
    def requirements(self) -> StrategyRequirements:
        """Return the configured requirements."""
        return self._reqs

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        """Return the configured configuration type."""
        return self._configuration_type

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: object,
    ) -> StrategyEvaluation:
        """Record the call and return an outcome per the evaluator or configured behavior."""
        self.calls.append(context)
        if self._evaluator is not None:
            return self._evaluator(context)
        if self._behavior == "raise":
            raise RuntimeError("strategy blew up")
        if self._behavior == "inconsistent":
            return StrategyEvaluation(
                instrument=context.instrument,
                context_version=context.version + 1,
                status=EvaluationStatus.NO_MATCH,
            )
        if self._behavior == "match":
            return StrategyEvaluation(
                instrument=context.instrument,
                context_version=context.version,
                status=EvaluationStatus.MATCHED,
                reason_codes=("MATCH",),
            )
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.NO_MATCH,
        )


def running_lifecycle(*strategy_ids: str) -> StrategyLifecycle:
    """Return a lifecycle with each id registered, started, and RUNNING."""
    lifecycle = StrategyLifecycle()
    for strategy_id in strategy_ids:
        lifecycle.register(strategy_id)
        lifecycle.start(strategy_id)
        lifecycle.mark_running(strategy_id)
    return lifecycle


def default_configs(
    *strategy_ids: str,
    factory: Callable[[], StrategyConfiguration] = lambda: StrategyConfiguration(
        config_version="1.0.0"
    ),
) -> dict[str, StrategyConfiguration]:
    """Return a config mapping supplying a valid configuration for each id."""
    return {strategy_id: factory() for strategy_id in strategy_ids}
