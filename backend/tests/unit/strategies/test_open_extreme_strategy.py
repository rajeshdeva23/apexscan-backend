"""Open=High / Open=Low current-session strategy + readiness + scanner tests (DEPLOY-10).

Deterministic and offline: no live Dhan, no wall-clock sleeps. Authoritative
SessionStatistics is injected into MarketContext; scanner behaviour is driven by
publishing StrategyResult events on an in-process EventBus.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.events.bus import EventBus
from app.market_engine.context import (
    MarketContext,
    MarketState,
    SessionContext,
    SessionStatistics,
    SessionStatisticsQuality,
)
from app.schemas.market_data import Instrument
from app.services.cross_instrument_scanner import (
    CrossInstrumentStrategyScanner,
    ScannerOrdering,
    ScannerRankingPolicy,
    ScannerRankingPolicyRegistry,
)
from app.services.strategy_catalog import production_catalog
from app.strategies.contracts import StrategyEvaluationMetadata
from app.strategies.enums import EvaluationStatus, FactNeed, StrategyTrigger
from app.strategies.implementations.open_extreme import (
    OpenExtremeConfiguration,
    OpenHighStrategy,
    OpenLowStrategy,
)
from app.strategies.results import StrategyResult
from app.strategy_manager.events import StrategyResultsPublished
from app.strategy_manager.readiness import assess_readiness
from app.strategy_manager.records import Readiness

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
_DATE = date(2026, 8, 28)
_NEXT_DATE = date(2026, 8, 31)
_AS_OF = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
_CONFIG = OpenExtremeConfiguration(config_version="1.0.0")
_METADATA = StrategyEvaluationMetadata(
    trigger=StrategyTrigger.ON_TICK, context_version=1, observed_at=_AS_OF, trading_date=_DATE
)


def _stats(
    open_p: str | None,
    high_p: str | None,
    low_p: str | None,
    *,
    quality: SessionStatisticsQuality = SessionStatisticsQuality.AUTHORITATIVE,
    trading_date: date = _DATE,
    as_of: datetime = _AS_OF,
) -> SessionStatistics:
    return SessionStatistics(
        trading_date=trading_date,
        open_price=Decimal(open_p) if open_p is not None else None,
        high_price=Decimal(high_p) if high_p is not None else None,
        low_price=Decimal(low_p) if low_p is not None else None,
        quality=quality,
        as_of=as_of,
    )


def _context(
    statistics: SessionStatistics | None,
    *,
    instrument: Instrument = _INSTRUMENT,
    trading_date: date | None = _DATE,
    observed_at: datetime = _AS_OF,
) -> MarketContext:
    session = (
        SessionContext(
            trading_date=trading_date,
            market_state=MarketState.LIVE_SESSION,
            exchange_timezone="Asia/Kolkata",
        )
        if trading_date is not None
        else None
    )
    return MarketContext.initial(
        instrument,
        sequence=1,
        event_timestamp=observed_at,
        observed_at=observed_at,
        latest_tick=None,
        session=session,
        session_statistics=statistics,
    )


# --------------------------------------------------------------------------- #
# OH — Open=High evaluation
# --------------------------------------------------------------------------- #
def test_oh_01_match_when_open_equals_high() -> None:
    result = OpenHighStrategy().evaluate(_context(_stats("100", "100", "95")), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.MATCHED
    assert "OPEN_EQUALS_HIGH" in result.reason_codes
    metric = {m.name: m.value for m in result.metrics}
    assert metric["session_range_pct"] == Decimal("100") - Decimal("95")  # (100-95)/100*100 = 5
    assert metric["session_range_pct"] == Decimal("5")


def test_oh_02_no_match_when_high_above_open() -> None:
    result = OpenHighStrategy().evaluate(_context(_stats("100", "105", "95")), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.NO_MATCH
    assert "HIGH_ABOVE_OPEN" in result.reason_codes


def test_oh_03_invalidates_when_high_increases() -> None:
    strategy = OpenHighStrategy()
    first = strategy.evaluate(_context(_stats("100", "100", "98")), _CONFIG, _METADATA)
    assert first.status is EvaluationStatus.MATCHED
    later = strategy.evaluate(_context(_stats("100", "101", "98")), _CONFIG, _METADATA)
    assert later.status is EvaluationStatus.NO_MATCH


def test_oh_04_fails_closed_when_statistics_unavailable() -> None:
    result = OpenHighStrategy().evaluate(_context(None), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.SKIPPED
    assert "SESSION_STATISTICS_UNAVAILABLE" in result.reason_codes


def test_oh_05_fails_closed_when_not_authoritative() -> None:
    unavailable = _stats(None, None, None, quality=SessionStatisticsQuality.UNAVAILABLE)
    result = OpenHighStrategy().evaluate(_context(unavailable), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.SKIPPED


def test_oh_exact_decimal_equality_ignores_trailing_zeros() -> None:
    result = OpenHighStrategy().evaluate(
        _context(_stats("100.00", "100.0", "99")), _CONFIG, _METADATA
    )
    assert result.status is EvaluationStatus.MATCHED


# --------------------------------------------------------------------------- #
# OL — Open=Low evaluation
# --------------------------------------------------------------------------- #
def test_ol_01_match_when_open_equals_low() -> None:
    result = OpenLowStrategy().evaluate(_context(_stats("100", "108", "100")), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.MATCHED
    assert "OPEN_EQUALS_LOW" in result.reason_codes
    metric = {m.name: m.value for m in result.metrics}
    assert metric["session_range_pct"] == Decimal("8")  # (108-100)/100*100


def test_ol_02_no_match_when_low_below_open() -> None:
    result = OpenLowStrategy().evaluate(_context(_stats("100", "108", "95")), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.NO_MATCH
    assert "LOW_BELOW_OPEN" in result.reason_codes


def test_ol_03_invalidates_when_low_decreases() -> None:
    strategy = OpenLowStrategy()
    first = strategy.evaluate(_context(_stats("100", "104", "100")), _CONFIG, _METADATA)
    assert first.status is EvaluationStatus.MATCHED
    later = strategy.evaluate(_context(_stats("100", "104", "99")), _CONFIG, _METADATA)
    assert later.status is EvaluationStatus.NO_MATCH


def test_ol_04_fails_closed_when_statistics_unavailable() -> None:
    result = OpenLowStrategy().evaluate(_context(None), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.SKIPPED


def test_ol_05_fails_closed_when_not_authoritative() -> None:
    unavailable = _stats(None, None, None, quality=SessionStatisticsQuality.UNAVAILABLE)
    result = OpenLowStrategy().evaluate(_context(unavailable), _CONFIG, _METADATA)
    assert result.status is EvaluationStatus.SKIPPED


# --------------------------------------------------------------------------- #
# AUTH-STRAT — readiness gate enforces authority (single enforcement point)
# --------------------------------------------------------------------------- #
def _assess(strategy: object, context: MarketContext) -> Readiness:
    return assess_readiness(
        descriptor=strategy.descriptor,  # type: ignore[attr-defined]
        requirements=strategy.requirements,  # type: ignore[attr-defined]
        configuration=_CONFIG,
        configuration_type=OpenExtremeConfiguration,
        context=context,
    )


def test_auth_strat_01_readiness_blocks_when_authority_unavailable() -> None:
    unavailable = _stats(None, None, None, quality=SessionStatisticsQuality.UNAVAILABLE)
    verdict = _assess(OpenHighStrategy(), _context(unavailable))
    assert verdict is Readiness.SESSION_STATISTICS_NOT_AUTHORITATIVE


def test_auth_strat_01b_readiness_blocks_when_statistics_missing() -> None:
    verdict = _assess(OpenLowStrategy(), _context(None))
    assert verdict is Readiness.MISSING_SESSION_STATISTICS


def test_auth_strat_02_readiness_admits_when_authoritative_and_fresh() -> None:
    context = _context(_stats("100", "100", "98"), observed_at=_AS_OF + timedelta(seconds=10))
    assert _assess(OpenHighStrategy(), context) is Readiness.READY


def test_requirements_declare_session_statistics_with_freshness() -> None:
    requirements = OpenHighStrategy().requirements
    assert FactNeed.SESSION_STATISTICS in requirements.fact_needs
    freshness_facts = {entry.fact for entry in requirements.freshness}
    assert FactNeed.SESSION_STATISTICS in freshness_facts


# --------------------------------------------------------------------------- #
# Catalog registration (offline; production STRATEGIES_ENABLED unchanged)
# --------------------------------------------------------------------------- #
def test_catalog_registers_open_high_and_open_low_as_scannable() -> None:
    catalog = production_catalog()
    entries = catalog.resolve(["open_high", "open_low"])
    ids = [entry.strategy_id for entry in entries]
    assert ids == ["open_high", "open_low"]
    for entry in entries:
        assert entry.ranking_policy is not None
        assert entry.ranking_policy.metric_name == "session_range_pct"
        assert entry.ranking_policy.ordering is ScannerOrdering.DESCENDING


# --------------------------------------------------------------------------- #
# Scanner integration — MATCH appears, MATCH→NO_MATCH removes, isolation
# --------------------------------------------------------------------------- #
_POLICY = ScannerRankingPolicy("open_high", "session_range_pct", ScannerOrdering.DESCENDING)


def _result_from(
    context: MarketContext, *, context_version: int, strategy: object = OpenHighStrategy()
) -> StrategyResult:
    evaluation = strategy.evaluate(context, _CONFIG, _METADATA)  # type: ignore[attr-defined]
    return StrategyResult(
        strategy_id="open_high",
        strategy_version="1.0.0",
        config_version="1.0.0",
        instrument=context.instrument,
        context_version=context_version,
        evaluation_timestamp=_AS_OF,
        status=evaluation.status,
        reason_codes=evaluation.reason_codes,
        metrics=evaluation.metrics,
    )


def _scanner(*symbols: str) -> tuple[CrossInstrumentStrategyScanner, EventBus]:
    universe = tuple(Instrument(exchange="NSE", symbol=s) for s in symbols)
    bus = EventBus()
    scanner = CrossInstrumentStrategyScanner(
        instruments=universe, policies=ScannerRankingPolicyRegistry((_POLICY,)), bus=bus
    )
    scanner.subscribe()
    return scanner, bus


def _publish(bus: EventBus, result: StrategyResult) -> None:
    bus.publish(
        StrategyResultsPublished(
            instrument=result.instrument,
            context_version=result.context_version,
            results=(result,),
            ranked=(),
            trading_date=_DATE,
        )
    )


def test_scanner_01_match_appears_in_snapshot() -> None:
    scanner, bus = _scanner("RELIANCE")
    context = _context(
        _stats("100", "100", "95"), instrument=Instrument(exchange="NSE", symbol="RELIANCE")
    )
    _publish(bus, _result_from(context, context_version=1))
    snapshot = scanner.snapshot("open_high")
    assert snapshot is not None
    assert [c.instrument.symbol for c in snapshot.candidates] == ["RELIANCE"]


def test_scanner_02_invalidation_removes_candidate() -> None:
    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    scanner, bus = _scanner("RELIANCE")
    _publish(
        bus,
        _result_from(
            _context(_stats("100", "100", "95"), instrument=instrument), context_version=1
        ),
    )
    assert scanner.snapshot("open_high").candidates  # matched → present
    # A later context where a new high appeared above the open → NO_MATCH.
    _publish(
        bus,
        _result_from(
            _context(_stats("100", "101", "95"), instrument=instrument), context_version=2
        ),
    )
    snapshot = scanner.snapshot("open_high")
    assert snapshot is not None
    assert snapshot.candidates == ()  # invalidated candidate removed from ranking


def test_scanner_03_other_instruments_unaffected_by_one_invalidation() -> None:
    aaa = Instrument(exchange="NSE", symbol="AAA")
    bbb = Instrument(exchange="NSE", symbol="BBB")
    scanner, bus = _scanner("AAA", "BBB")
    _publish(
        bus, _result_from(_context(_stats("100", "100", "95"), instrument=aaa), context_version=1)
    )
    _publish(
        bus, _result_from(_context(_stats("100", "100", "90"), instrument=bbb), context_version=1)
    )
    assert {c.instrument.symbol for c in scanner.snapshot("open_high").candidates} == {"AAA", "BBB"}
    # AAA invalidates; BBB remains.
    _publish(
        bus, _result_from(_context(_stats("100", "101", "95"), instrument=aaa), context_version=2)
    )
    remaining = [c.instrument.symbol for c in scanner.snapshot("open_high").candidates]
    assert remaining == ["BBB"]


def test_session_01_new_trading_date_does_not_reuse_prior_qualification() -> None:
    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    scanner, bus = _scanner("RELIANCE")
    _publish(
        bus,
        _result_from(
            _context(_stats("100", "100", "95"), instrument=instrument), context_version=1
        ),
    )
    assert scanner.snapshot("open_high").candidates  # today matched
    # Next trading day: a fresh snapshot; yesterday's qualification must not persist.
    next_result = StrategyResult(
        strategy_id="open_high",
        strategy_version="1.0.0",
        config_version="1.0.0",
        instrument=instrument,
        context_version=1,
        evaluation_timestamp=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        status=EvaluationStatus.NO_MATCH,
        reason_codes=(),
        metrics=(),
    )
    bus.publish(
        StrategyResultsPublished(
            instrument=instrument,
            context_version=1,
            results=(next_result,),
            ranked=(),
            trading_date=_NEXT_DATE,
        )
    )
    snapshot = scanner.snapshot("open_high")
    assert snapshot is not None
    assert snapshot.trading_date == _NEXT_DATE
    assert snapshot.candidates == ()
