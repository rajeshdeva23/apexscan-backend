"""Narrow CPR V1 strategy tests (ADR-007 Narrow CPR strategy specification)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.historical.context import HistoricalContext, PreviousSessionFacts
from app.schemas.market_data import Candle, Instrument, Tick
from app.strategies.contracts import Strategy, StrategyEvaluationMetadata
from app.strategies.enums import (
    EmissionPolicy,
    EvaluationStatus,
    FactNeed,
    StrategyCategory,
    StrategyTrigger,
)
from app.strategies.implementations.narrow_cpr import (
    NarrowCprConfiguration,
    NarrowCprStrategy,
)
from app.strategies.registry import StrategyRegistry
from app.strategies.results import MetricEntry

INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
PREV_DATE = date(2026, 2, 6)
EVENT = datetime(2026, 2, 9, 9, 15, tzinfo=UTC)
META = StrategyEvaluationMetadata(
    trigger=StrategyTrigger.ON_HISTORICAL_READY,
    context_version=1,
    observed_at=EVENT,
    trading_date=date(2026, 2, 9),
)
_DIRECTIONAL_TOKENS = {"BUY", "SELL", "LONG", "SHORT", "BULLISH", "BEARISH"}


def _candle(high: str, low: str, close: str, open_: str = "100") -> Candle:
    return Candle(
        instrument=INSTRUMENT,
        start_timestamp=EVENT,
        end_timestamp=EVENT + timedelta(hours=6),
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
        traded_quantity=100,
    )


def _historical(candle: Candle | None) -> HistoricalContext:
    previous = (
        PreviousSessionFacts(trading_date=PREV_DATE, candle=candle) if candle is not None else None
    )
    return HistoricalContext(instrument=INSTRUMENT, previous_session=previous)


def _context(
    *,
    historical: HistoricalContext | None,
    version: int = 1,
    latest_tick: Tick | None = None,
    session: SessionContext | None = None,
) -> MarketContext:
    return MarketContext(
        instrument=INSTRUMENT,
        version=version,
        sequence=version,
        event_timestamp=EVENT,
        observed_at=EVENT,
        latest_tick=latest_tick,
        session=session,
        historical=historical,
    )


def _config(threshold: str | None = None) -> NarrowCprConfiguration:
    return NarrowCprConfiguration(
        config_version="1.0.0",
        narrow_cpr_max_width_pct=Decimal(threshold) if threshold is not None else None,
    )


def _metrics(entries: tuple[MetricEntry, ...]) -> dict[str, object]:
    return {entry.name: entry.value for entry in entries}


# Canonical previous session: H=120 L=68 C=112 -> width_pct 12, TC=106>BC=94.
_NORMAL = _candle("120", "68", "112")


# G — descriptor.
def test_descriptor_identity() -> None:
    descriptor = NarrowCprStrategy().descriptor
    assert descriptor.strategy_id == "narrow_cpr"
    assert descriptor.version == "1.0.0"
    assert descriptor.category is StrategyCategory.MARKET_STRUCTURE
    assert descriptor.emission_policy is EmissionPolicy.ONE_SHOT_PER_SESSION


# H / I — requirements: one session lookback, previous-session fact, no live/session-stats.
def test_requirements_declare_single_session_lookback() -> None:
    reqs = NarrowCprStrategy().requirements
    assert len(reqs.historical) == 1
    requirement = reqs.historical[0]
    assert requirement.timeframe.is_session
    assert requirement.lookback == 1
    assert reqs.live_timeframes == ()
    assert FactNeed.PREVIOUS_SESSION in reqs.fact_needs
    assert FactNeed.SESSION_STATISTICS not in reqs.fact_needs
    assert reqs.trigger is StrategyTrigger.ON_HISTORICAL_READY


# J / M — valid previous session, no threshold -> MATCHED, score None.
def test_matched_without_threshold() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config(), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert evaluation.reason_codes == ("NARROW_CPR_VALID",)
    assert evaluation.score is None
    assert _metrics(evaluation.metrics)["cpr_width_pct"] == Decimal("12")


# K — threshold exact boundary matches (<=).
def test_threshold_boundary_matches() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config("12"), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert evaluation.reason_codes == ("NARROW_CPR_WITHIN_THRESHOLD",)


# L — threshold below the width -> NO_MATCH.
def test_threshold_below_width_no_match() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config("11"), META
    )
    assert evaluation.status is EvaluationStatus.NO_MATCH
    assert evaluation.reason_codes == ("NARROW_CPR_ABOVE_THRESHOLD",)
    assert evaluation.score is None


# N / R — metrics content incl. authoritative source-session date.
def test_metrics_content_and_source_date() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config(), META
    )
    metrics = _metrics(evaluation.metrics)
    assert metrics["pivot"] == Decimal("100")
    assert metrics["bc"] == Decimal("94")
    assert metrics["tc"] == Decimal("106")
    assert metrics["cpr_top"] == Decimal("106")
    assert metrics["cpr_bottom"] == Decimal("94")
    assert metrics["cpr_width"] == Decimal("12")
    assert metrics["previous_high"] == Decimal("120")
    assert metrics["previous_low"] == Decimal("68")
    assert metrics["previous_close"] == Decimal("112")
    assert metrics["source_session_date"] == "2026-02-06"


# O — non-directional: no directional reason codes or metric names.
def test_no_directional_output() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config(), META
    )
    assert not (set(evaluation.reason_codes) & _DIRECTIONAL_TOKENS)
    names = {entry.name.lower() for entry in evaluation.metrics}
    assert not (names & {"direction", "bias", "side", "long", "short"})


# TC<BC orientation through the strategy. H=140 L=72 C=88.
def test_tc_below_bc_through_strategy() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_candle("140", "72", "88"))), _config(), META
    )
    metrics = _metrics(evaluation.metrics)
    assert metrics["cpr_top"] == Decimal("106")
    assert metrics["cpr_bottom"] == Decimal("94")
    assert evaluation.status is EvaluationStatus.MATCHED


# Zero-width CPR through the strategy is valid and matches.
def test_zero_width_through_strategy() -> None:
    evaluation = NarrowCprStrategy().evaluate(
        _context(historical=_historical(_candle("110", "90", "100"))), _config(), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert _metrics(evaluation.metrics)["cpr_width_pct"] == Decimal("0")


# P — determinism: repeated evaluation is exactly equal.
def test_deterministic_repeated_evaluation() -> None:
    strategy = NarrowCprStrategy()
    context = _context(historical=_historical(_NORMAL))
    assert strategy.evaluate(context, _config(), META) == strategy.evaluate(
        context, _config(), META
    )


# Q — no look-ahead: differing current-day/live fields do not change the result.
def test_live_variation_does_not_change_result() -> None:
    strategy = NarrowCprStrategy()
    calm = _context(historical=_historical(_NORMAL))
    noisy = _context(
        historical=_historical(_NORMAL),
        latest_tick=Tick(instrument=INSTRUMENT, event_timestamp=EVENT, last_price=Decimal("999")),
        session=SessionContext(
            trading_date=date(2026, 2, 9),
            market_state=MarketState.LIVE_SESSION,
            exchange_timezone="Asia/Kolkata",
        ),
    )
    assert strategy.evaluate(calm, _config(), META) == strategy.evaluate(noisy, _config(), META)


# T — multi-interval transparency: identical canonical OHLC -> identical result.
def test_multi_interval_candle_is_transparent() -> None:
    strategy = NarrowCprStrategy()
    # A candle whose OHLC equals a multi-interval-aggregated session (first open, max high,
    # min low, last close) is indistinguishable to the strategy from any other candle
    # carrying the same OHLC — it has no interval awareness.
    aggregated = _candle("120", "68", "112", open_="70")
    single = _candle("120", "68", "112", open_="100")
    left = strategy.evaluate(_context(historical=_historical(aggregated)), _config(), META)
    right = strategy.evaluate(_context(historical=_historical(single)), _config(), META)
    assert _metrics(left.metrics) == _metrics(right.metrics)
    assert left.status is right.status


# S — missing previous session fails closed (SKIPPED), never fabricates a result.
def test_missing_previous_session_skips() -> None:
    strategy = NarrowCprStrategy()
    absent_context = strategy.evaluate(_context(historical=None), _config(), META)
    assert absent_context.status is EvaluationStatus.SKIPPED
    assert absent_context.reason_codes == ("NARROW_CPR_NO_PREVIOUS_SESSION",)
    assert absent_context.metrics == ()
    empty_history = strategy.evaluate(_context(historical=_historical(None)), _config(), META)
    assert empty_history.status is EvaluationStatus.SKIPPED


# Plug-and-play: conforms to the Strategy protocol and registers normally.
def test_registers_as_a_plugin() -> None:
    strategy = NarrowCprStrategy()
    assert isinstance(strategy, Strategy)
    registry = StrategyRegistry()
    registry.register(strategy)
    assert registry.get("narrow_cpr") is strategy
