"""Previous Session Relative Range V1 strategy tests (ADR-007 PSRR spec §22)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_engine.context import MarketContext, SessionContext
from app.market_engine.historical.context import (
    HistoricalContext,
    HistoricalSeries,
    PreviousSessionFacts,
)
from app.market_engine.timeframe import Timeframe
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
from app.strategies.implementations.previous_session_relative_range import (
    PreviousSessionRelativeRangeConfiguration,
    PreviousSessionRelativeRangeStrategy,
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
_DIRECTIONAL = {"BUY", "SELL", "LONG", "SHORT", "BULLISH", "BEARISH"}


def _candle(index: int, range_value: str) -> Candle:
    start = EVENT + timedelta(days=index)
    return Candle(
        instrument=INSTRUMENT,
        start_timestamp=start,
        end_timestamp=start + timedelta(hours=6),
        open_price=Decimal("100"),
        high_price=Decimal("100") + Decimal(range_value),
        low_price=Decimal("100"),
        close_price=Decimal("100"),
        traded_quantity=100,
    )


def _series(baseline_ranges: list[str], subject_range: str) -> HistoricalSeries:
    ranges = [*baseline_ranges, subject_range]  # oldest -> newest; subject is D-1 (last)
    candles = tuple(_candle(i, r) for i, r in enumerate(ranges))
    return HistoricalSeries(timeframe=Timeframe.session(), candles=candles)


def _context(
    *,
    series: HistoricalSeries | None,
    with_previous: bool = True,
    latest_tick: Tick | None = None,
    session: SessionContext | None = None,
) -> MarketContext:
    if series is None:
        historical = HistoricalContext(instrument=INSTRUMENT, previous_session=None, series=())
    else:
        previous = (
            PreviousSessionFacts(trading_date=PREV_DATE, candle=series.candles[-1])
            if with_previous
            else None
        )
        historical = HistoricalContext(
            instrument=INSTRUMENT, previous_session=previous, series=(series,)
        )
    return MarketContext(
        instrument=INSTRUMENT,
        version=1,
        sequence=1,
        event_timestamp=EVENT,
        observed_at=EVENT,
        latest_tick=latest_tick,
        session=session,
        historical=historical,
    )


def _config() -> PreviousSessionRelativeRangeConfiguration:
    return PreviousSessionRelativeRangeConfiguration(config_version="1.0.0")


def _metrics(entries: tuple[MetricEntry, ...]) -> dict[str, object]:
    return {entry.name: entry.value for entry in entries}


# baseline: twenty sessions of range 20; subject range 10 -> ratio 0.5.
_NORMAL = _series(["20"] * 20, "10")


def test_descriptor_identity() -> None:
    descriptor = PreviousSessionRelativeRangeStrategy().descriptor
    assert descriptor.strategy_id == "previous_session_relative_range"
    assert descriptor.version == "1.0.0"
    assert descriptor.category is StrategyCategory.MARKET_STRUCTURE
    assert descriptor.emission_policy is EmissionPolicy.ONE_SHOT_PER_SESSION


def test_requirements_declare_21_session_lookback() -> None:
    reqs = PreviousSessionRelativeRangeStrategy().requirements
    assert len(reqs.historical) == 1
    assert reqs.historical[0].timeframe.is_session
    assert reqs.historical[0].lookback == 21
    assert reqs.live_timeframes == ()
    assert FactNeed.PREVIOUS_SESSION in reqs.fact_needs
    assert FactNeed.SESSION_STATISTICS not in reqs.fact_needs
    assert reqs.trigger is StrategyTrigger.ON_HISTORICAL_READY


def test_matched_ratio_and_metrics() -> None:
    evaluation = PreviousSessionRelativeRangeStrategy().evaluate(
        _context(series=_NORMAL), _config(), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert evaluation.reason_codes == ("PREVIOUS_SESSION_RELATIVE_RANGE_VALID",)
    assert evaluation.score is None
    metrics = _metrics(evaluation.metrics)
    assert metrics["relative_range_ratio"] == Decimal("0.5")
    assert metrics["previous_range_pct"] == Decimal("10")
    assert metrics["baseline_range_pct"] == Decimal("20")
    assert metrics["baseline_sessions"] == 20
    assert metrics["source_session_date"] == "2026-02-06"


def test_subject_excluded_from_baseline() -> None:
    # subject range 2 must NOT lower the baseline; baseline stays median of the twenty 20s.
    series = _series(["20"] * 20, "2")
    metrics = _metrics(
        PreviousSessionRelativeRangeStrategy()
        .evaluate(_context(series=series), _config(), META)
        .metrics
    )
    assert metrics["baseline_range_pct"] == Decimal("20")
    assert metrics["previous_range_pct"] == Decimal("2")
    assert metrics["relative_range_ratio"] == Decimal("0.1")


def test_zero_subject_range_is_valid_ratio_zero() -> None:
    series = _series(["20"] * 20, "0")
    evaluation = PreviousSessionRelativeRangeStrategy().evaluate(
        _context(series=series), _config(), META
    )
    assert evaluation.status is EvaluationStatus.MATCHED
    assert _metrics(evaluation.metrics)["relative_range_ratio"] == Decimal("0")


def test_degenerate_baseline_is_skipped() -> None:
    series = _series(["0"] * 20, "5")
    evaluation = PreviousSessionRelativeRangeStrategy().evaluate(
        _context(series=series), _config(), META
    )
    assert evaluation.status is EvaluationStatus.SKIPPED
    assert evaluation.reason_codes == ("PREVIOUS_SESSION_RELATIVE_RANGE_DEGENERATE_BASELINE",)


def test_missing_history_wrong_length_is_skipped() -> None:
    short = _series(["20"] * 19, "10")  # only 20 candles total
    evaluation = PreviousSessionRelativeRangeStrategy().evaluate(
        _context(series=short), _config(), META
    )
    assert evaluation.status is EvaluationStatus.SKIPPED
    assert evaluation.reason_codes == ("PREVIOUS_SESSION_RELATIVE_RANGE_NO_HISTORY",)


def test_absent_series_is_skipped() -> None:
    evaluation = PreviousSessionRelativeRangeStrategy().evaluate(
        _context(series=None), _config(), META
    )
    assert evaluation.status is EvaluationStatus.SKIPPED
    assert evaluation.reason_codes == ("PREVIOUS_SESSION_RELATIVE_RANGE_NO_HISTORY",)


def test_no_directional_output() -> None:
    evaluation = PreviousSessionRelativeRangeStrategy().evaluate(
        _context(series=_NORMAL), _config(), META
    )
    assert not (set(evaluation.reason_codes) & _DIRECTIONAL)
    names = {entry.name.lower() for entry in evaluation.metrics}
    assert not (names & {"direction", "bias", "side", "long", "short"})


def test_no_look_ahead_current_session_does_not_change_result() -> None:
    strategy = PreviousSessionRelativeRangeStrategy()
    bare = strategy.evaluate(_context(series=_NORMAL), _config(), META)
    tick = Tick(
        instrument=INSTRUMENT, event_timestamp=EVENT, last_price=Decimal("999"), traded_quantity=10
    )
    with_today = strategy.evaluate(_context(series=_NORMAL, latest_tick=tick), _config(), META)
    assert _metrics(bare.metrics) == _metrics(with_today.metrics)


def test_deterministic_repeated_evaluation() -> None:
    strategy = PreviousSessionRelativeRangeStrategy()
    context = _context(series=_NORMAL)
    assert strategy.evaluate(context, _config(), META) == strategy.evaluate(
        context, _config(), META
    )


def test_wrong_configuration_type_fails_closed() -> None:
    class _Other(StrategyConfiguration):
        pass

    with pytest.raises(TypeError):
        PreviousSessionRelativeRangeStrategy().evaluate(
            _context(series=_NORMAL), _Other(config_version="1.0.0"), META
        )
