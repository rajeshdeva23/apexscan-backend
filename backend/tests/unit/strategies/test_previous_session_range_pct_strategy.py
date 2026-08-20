"""Previous Session Range % V1 strategy tests (ADR-007 PSR strategy specification)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_engine.context import MarketContext, SessionContext
from app.market_engine.historical.context import HistoricalContext, PreviousSessionFacts
from app.schemas.market_data import Candle, Instrument, Tick
from app.strategies.configuration import StrategyConfiguration
from app.strategies.contracts import StrategyEvaluationMetadata
from app.strategies.enums import (
    EmissionPolicy,
    EvaluationStatus,
    FactNeed,
    StrategyCategory,
    StrategyTrigger,
)
from app.strategies.implementations.previous_session_range_pct import (
    PreviousSessionRangePctConfiguration,
    PreviousSessionRangePctStrategy,
)
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


def _candle(open_: str, high: str, low: str, close: str) -> Candle:
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


def _config() -> PreviousSessionRangePctConfiguration:
    return PreviousSessionRangePctConfiguration(config_version="1.0.0")


def _metrics(entries: tuple[MetricEntry, ...]) -> dict[str, object]:
    return {entry.name: entry.value for entry in entries}


# open=100 high=105 low=95 close=100 -> range 10, range_pct 10.
_NORMAL = _candle("100", "105", "95", "100")


def test_descriptor_identity() -> None:
    descriptor = PreviousSessionRangePctStrategy().descriptor
    assert descriptor.strategy_id == "previous_session_range_pct"
    assert descriptor.version == "1.0.0"
    assert descriptor.category is StrategyCategory.MARKET_STRUCTURE
    assert descriptor.emission_policy is EmissionPolicy.ONE_SHOT_PER_SESSION


def test_requirements_declare_single_session_lookback() -> None:
    reqs = PreviousSessionRangePctStrategy().requirements
    assert len(reqs.historical) == 1
    requirement = reqs.historical[0]
    assert requirement.timeframe.is_session
    assert requirement.lookback == 1
    assert reqs.live_timeframes == ()
    assert FactNeed.PREVIOUS_SESSION in reqs.fact_needs
    assert FactNeed.SESSION_STATISTICS not in reqs.fact_needs
    assert reqs.trigger is StrategyTrigger.ON_HISTORICAL_READY


def test_matched_rank_all_score_none() -> None:
    evaluation = PreviousSessionRangePctStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config(), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert evaluation.reason_codes == ("PREVIOUS_SESSION_RANGE_VALID",)
    assert evaluation.score is None
    assert _metrics(evaluation.metrics)["previous_range_pct"] == Decimal("10")


def test_metrics_content_and_source_date() -> None:
    metrics = _metrics(
        PreviousSessionRangePctStrategy()
        .evaluate(_context(historical=_historical(_NORMAL)), _config(), META)
        .metrics
    )
    assert metrics["previous_range_pct"] == Decimal("10")
    assert metrics["previous_range"] == Decimal("10")
    assert metrics["previous_open"] == Decimal("100")
    assert metrics["previous_high"] == Decimal("105")
    assert metrics["previous_low"] == Decimal("95")
    assert metrics["previous_close"] == Decimal("100")
    assert metrics["source_session_date"] == "2026-02-06"


def test_missing_previous_session_is_skipped() -> None:
    evaluation = PreviousSessionRangePctStrategy().evaluate(
        _context(historical=_historical(None)), _config(), META
    )
    assert evaluation.status is EvaluationStatus.SKIPPED
    assert evaluation.reason_codes == ("PREVIOUS_SESSION_RANGE_NO_PREVIOUS",)


def test_zero_range_through_strategy_matches() -> None:
    evaluation = PreviousSessionRangePctStrategy().evaluate(
        _context(historical=_historical(_candle("100", "100", "100", "100"))), _config(), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert _metrics(evaluation.metrics)["previous_range_pct"] == Decimal("0")


def test_no_directional_output() -> None:
    evaluation = PreviousSessionRangePctStrategy().evaluate(
        _context(historical=_historical(_NORMAL)), _config(), META
    )
    assert not (set(evaluation.reason_codes) & _DIRECTIONAL_TOKENS)
    names = {entry.name.lower() for entry in evaluation.metrics}
    assert not (names & {"direction", "bias", "side", "long", "short"})


def test_no_look_ahead_current_session_does_not_change_result() -> None:
    """Today's tick/session must not alter the previous-session-only metric."""
    strategy = PreviousSessionRangePctStrategy()
    bare = strategy.evaluate(_context(historical=_historical(_NORMAL)), _config(), META)
    tick = Tick(
        instrument=INSTRUMENT,
        event_timestamp=EVENT,
        last_price=Decimal("999"),
        traded_quantity=10,
    )
    with_today = strategy.evaluate(
        _context(historical=_historical(_NORMAL), latest_tick=tick), _config(), META
    )
    assert _metrics(bare.metrics) == _metrics(with_today.metrics)


def test_deterministic_repeated_evaluation() -> None:
    strategy = PreviousSessionRangePctStrategy()
    context = _context(historical=_historical(_NORMAL))
    assert strategy.evaluate(context, _config(), META) == strategy.evaluate(
        context, _config(), META
    )


def test_wrong_configuration_type_fails_closed() -> None:
    class _Other(StrategyConfiguration):
        pass

    with pytest.raises(TypeError):
        PreviousSessionRangePctStrategy().evaluate(
            _context(historical=_historical(_NORMAL)), _Other(config_version="1.0.0"), META
        )
