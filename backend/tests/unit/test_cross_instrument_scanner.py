"""Generic cross-instrument strategy scanner tests (ADR-012)."""

from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import UTC, date, datetime
from decimal import Decimal

from app.events.bus import EventBus
from app.schemas.market_data import Instrument
from app.services import cross_instrument_scanner as scanner_module
from app.services.cross_instrument_scanner import (
    CrossInstrumentStrategyScanner,
    ScannerCandidate,
    ScannerOrdering,
    ScannerRankingPolicy,
    ScannerRankingPolicyRegistry,
    ScannerSnapshot,
    ScannerSnapshotCompleteness,
)
from app.strategies.enums import EvaluationStatus
from app.strategies.results import MetricEntry, StrategyResult
from app.strategy_manager.events import StrategyResultsPublished

TD = date(2026, 2, 9)
NEXT_TD = date(2026, 2, 10)
EVENT = datetime(2026, 2, 9, 4, 0, tzinfo=UTC)
_CPR = ScannerRankingPolicy("narrow_cpr", "cpr_width_pct", ScannerOrdering.ASCENDING)
_MOMENTUM = ScannerRankingPolicy("fake_momentum", "momentum_strength", ScannerOrdering.DESCENDING)
_DIRECTIONAL = {"direction", "bias", "side", "long", "short", "buy", "sell", "bullish", "bearish"}


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _result(
    symbol: str,
    metric: str | None,
    *,
    strategy_id: str = "narrow_cpr",
    metric_name: str = "cpr_width_pct",
    status: EvaluationStatus = EvaluationStatus.MATCHED,
    context_version: int = 1,
    strategy_version: str = "1.0.0",
    config_version: str = "1.0.0",
    metric_value: object | None = None,
) -> StrategyResult:
    if metric is not None:
        metrics: tuple[MetricEntry, ...] = (MetricEntry(name=metric_name, value=Decimal(metric)),)
    elif metric_value is not None:
        metrics = (MetricEntry(name=metric_name, value=metric_value),)
    else:
        metrics = ()
    return StrategyResult(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        config_version=config_version,
        instrument=_instrument(symbol),
        context_version=context_version,
        evaluation_timestamp=EVENT,
        status=status,
        reason_codes=("X",) if status is EvaluationStatus.MATCHED else (),
        metrics=metrics,
    )


def _event(result: StrategyResult, *, trading_date: date | None = TD) -> StrategyResultsPublished:
    return StrategyResultsPublished(
        instrument=result.instrument,
        context_version=result.context_version,
        results=(result,),
        ranked=(),
        trading_date=trading_date,
    )


def _scanner(
    symbols: tuple[str, ...], policies: tuple[ScannerRankingPolicy, ...] = (_CPR,)
) -> tuple[CrossInstrumentStrategyScanner, EventBus]:
    universe = tuple(_instrument(symbol) for symbol in symbols)
    bus = EventBus()
    scanner = CrossInstrumentStrategyScanner(
        instruments=universe, policies=ScannerRankingPolicyRegistry(policies), bus=bus
    )
    scanner.subscribe()
    return scanner, bus


def _symbols(snapshot: ScannerSnapshot) -> list[str]:
    return [candidate.instrument.symbol for candidate in snapshot.candidates]


# A / B — collect across instruments, ascending-width ranking (narrowest = rank 1).
def test_ascending_width_ranking() -> None:
    scanner, bus = _scanner(("AAA", "BBB", "CCC"))
    for symbol, width in (("AAA", "0.03"), ("BBB", "0.01"), ("CCC", "0.02")):
        bus.publish(_event(_result(symbol, width)))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert _symbols(snapshot) == ["BBB", "CCC", "AAA"]
    assert [candidate.rank for candidate in snapshot.candidates] == [1, 2, 3]
    assert snapshot.candidates[0].ranking_metric_value == Decimal("0.01")


# C — deterministic tie-break: equal width → ascending canonical instrument (exchange, symbol).
def test_tie_break_by_instrument_ascending() -> None:
    scanner, bus = _scanner(("ZZZ", "AAA"))
    bus.publish(_event(_result("ZZZ", "0.02")))
    bus.publish(_event(_result("AAA", "0.02")))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert _symbols(snapshot) == ["AAA", "ZZZ"]


# D — arrival order is irrelevant to the ranking.
def test_arrival_order_independence() -> None:
    order_one = (("AAA", "0.03"), ("BBB", "0.01"), ("CCC", "0.02"))
    order_two = (("CCC", "0.02"), ("AAA", "0.03"), ("BBB", "0.01"))
    ranked = []
    for order in (order_one, order_two):
        scanner, bus = _scanner(("AAA", "BBB", "CCC"))
        for symbol, width in order:
            bus.publish(_event(_result(symbol, width)))
        snapshot = scanner.snapshot("narrow_cpr")
        assert snapshot is not None
        ranked.append(_symbols(snapshot))
    assert ranked[0] == ranked[1] == ["BBB", "CCC", "AAA"]


# E / R — a single MATCHED result is eligible and ranked #1.
def test_single_matched_is_rank_one() -> None:
    scanner, bus = _scanner(("AAA",))
    bus.publish(_event(_result("AAA", "0.05")))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.eligible_count == 1
    assert snapshot.candidates[0].rank == 1


# F / Q — NO_MATCH is evaluated but not eligible; zero eligible is valid.
def test_no_match_is_evaluated_not_ranked() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    bus.publish(_event(_result("AAA", None, status=EvaluationStatus.NO_MATCH)))
    bus.publish(_event(_result("BBB", None, status=EvaluationStatus.NO_MATCH)))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.evaluated_count == 2
    assert snapshot.eligible_count == 0
    assert snapshot.candidates == ()
    assert (
        snapshot.completeness is ScannerSnapshotCompleteness.COMPLETE
    )  # 2 evaluated == 2 expected


# G — SKIPPED/ERROR are not on the public pipeline; if fed, they are ignored.
def test_skipped_result_is_ignored() -> None:
    scanner, bus = _scanner(("AAA",))
    bus.publish(_event(_result("AAA", None, status=EvaluationStatus.SKIPPED)))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is None  # nothing recorded


# H / P — a MATCHED result missing its metric is evaluated but excluded (fail closed), not zeroed.
def test_missing_metric_fails_closed_without_affecting_others() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    bus.publish(_event(_result("AAA", None)))  # MATCHED but no cpr_width_pct metric
    bus.publish(_event(_result("BBB", "0.02")))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.evaluated_count == 2
    assert _symbols(snapshot) == ["BBB"]  # AAA excluded; BBB unaffected


# I — a non-Decimal ranking metric fails closed (never coerced).
def test_malformed_metric_fails_closed() -> None:
    scanner, bus = _scanner(("AAA",))
    bus.publish(_event(_result("AAA", None, metric_value="not-a-decimal")))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.eligible_count == 0


# K — duplicate identical result is idempotent.
def test_duplicate_result_is_idempotent() -> None:
    scanner, bus = _scanner(("AAA",))
    result = _result("AAA", "0.02")
    bus.publish(_event(result))
    bus.publish(_event(result))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.evaluated_count == 1
    assert snapshot.eligible_count == 1


# L — higher context_version replaces; a stale (older) one is ignored.
def test_context_version_replacement() -> None:
    scanner, bus = _scanner(("AAA",))
    bus.publish(_event(_result("AAA", "0.05", context_version=1)))
    bus.publish(_event(_result("AAA", "0.01", context_version=2)))
    bus.publish(_event(_result("AAA", "0.09", context_version=1)))  # stale, ignored
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.candidates[0].ranking_metric_value == Decimal("0.01")


# M / N / O — expected count and complete vs partial completeness.
def test_completeness_partial_then_complete() -> None:
    scanner, bus = _scanner(("AAA", "BBB", "CCC"))
    bus.publish(_event(_result("AAA", "0.01")))
    partial = scanner.snapshot("narrow_cpr")
    assert partial is not None
    assert partial.expected_count == 3
    assert partial.completeness is ScannerSnapshotCompleteness.PARTIAL
    bus.publish(_event(_result("BBB", "0.02")))
    bus.publish(_event(_result("CCC", "0.03")))
    complete = scanner.snapshot("narrow_cpr")
    assert complete is not None
    assert complete.completeness is ScannerSnapshotCompleteness.COMPLETE


# §31 — an unknown strategy (no registered policy) is ignored.
def test_unknown_strategy_is_ignored() -> None:
    scanner, bus = _scanner(("AAA",), policies=())  # no policies registered
    bus.publish(_event(_result("AAA", "0.02")))
    assert scanner.snapshot("narrow_cpr") is None


# §32 — a result for an instrument outside the configured universe is ignored.
def test_instrument_outside_universe_is_ignored() -> None:
    scanner, bus = _scanner(("AAA",))
    bus.publish(_event(_result("OUTSIDER", "0.02")))
    assert scanner.snapshot("narrow_cpr") is None


# §24 — a same-day result with a conflicting config_version is ignored (fail closed).
def test_config_version_conflict_is_ignored() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    bus.publish(_event(_result("AAA", "0.01", config_version="1.0.0")))
    bus.publish(_event(_result("BBB", "0.02", config_version="2.0.0")))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.config_version == "1.0.0"
    assert snapshot.evaluated_count == 1  # the conflicting-config result never entered


# §23 — a new trading date advances the bounded snapshot; the prior day is not mixed in.
def test_trading_day_rollover_replaces_snapshot() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    bus.publish(_event(_result("AAA", "0.05"), trading_date=TD))
    bus.publish(_event(_result("BBB", "0.01"), trading_date=NEXT_TD))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.trading_date == NEXT_TD
    assert _symbols(snapshot) == ["BBB"]  # only the new day's candidate
    # A stale (older-day) result after rollover is ignored.
    bus.publish(_event(_result("AAA", "0.001"), trading_date=TD))
    after = scanner.snapshot("narrow_cpr")
    assert after is not None
    assert _symbols(after) == ["BBB"]


# trading_date None cannot establish snapshot identity → ignored.
def test_missing_trading_date_is_ignored() -> None:
    scanner, bus = _scanner(("AAA",))
    bus.publish(_event(_result("AAA", "0.02"), trading_date=None))
    assert scanner.snapshot("narrow_cpr") is None


# §41 — plug-and-play: one scanner ranks two strategies via their registered policies.
def test_plug_and_play_two_strategies() -> None:
    scanner, bus = _scanner(("AAA", "BBB", "CCC"), policies=(_CPR, _MOMENTUM))
    for symbol, width in (("AAA", "0.03"), ("BBB", "0.01"), ("CCC", "0.02")):
        bus.publish(_event(_result(symbol, width)))
    for symbol, strength in (("AAA", "10"), ("BBB", "30"), ("CCC", "20")):
        bus.publish(
            _event(
                _result(
                    symbol, strength, strategy_id="fake_momentum", metric_name="momentum_strength"
                )
            )
        )
    narrow = scanner.snapshot("narrow_cpr")
    momentum = scanner.snapshot("fake_momentum")
    assert narrow is not None and momentum is not None
    assert _symbols(narrow) == ["BBB", "CCC", "AAA"]  # ascending width
    assert _symbols(momentum) == ["BBB", "CCC", "AAA"]  # descending strength


# §40 / S — hundreds of instruments: all collected, deterministic, bounded.
def test_hundreds_of_instruments_bounded_and_deterministic() -> None:
    symbols = tuple(f"S{index:04d}" for index in range(300))
    scanner, bus = _scanner(symbols)
    for index, symbol in enumerate(symbols):
        bus.publish(_event(_result(symbol, f"0.{index:04d}")))
    snapshot = scanner.snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.expected_count == 300
    assert snapshot.eligible_count == 300
    assert len(snapshot.candidates) <= snapshot.expected_count
    assert snapshot.completeness is ScannerSnapshotCompleteness.COMPLETE
    assert [candidate.rank for candidate in snapshot.candidates] == list(range(1, 301))


# X — deterministic replay: identical inputs into two scanners produce equal snapshots.
def test_deterministic_replay() -> None:
    feed = (("AAA", "0.03"), ("BBB", "0.01"), ("CCC", "0.02"))
    snapshots = []
    for _ in range(2):
        scanner, bus = _scanner(("AAA", "BBB", "CCC"))
        for symbol, width in feed:
            bus.publish(_event(_result(symbol, width)))
        snapshots.append(scanner.snapshot("narrow_cpr"))
    assert snapshots[0] == snapshots[1]


# Registry conflict fails closed; identical re-registration is idempotent.
def test_policy_registry_conflict_and_idempotency() -> None:
    registry = ScannerRankingPolicyRegistry((_CPR,))
    registry.register(_CPR)  # idempotent
    assert registry.get("narrow_cpr") == _CPR
    try:
        registry.register(ScannerRankingPolicy("narrow_cpr", "other", ScannerOrdering.DESCENDING))
    except scanner_module.ScannerPolicyConflictError:
        pass
    else:  # pragma: no cover - the conflicting registration must raise
        raise AssertionError("conflicting policy registration must fail closed")


# Z — the scanner result contracts carry no directional field.
def test_no_directional_fields() -> None:
    candidate_fields = {field.name.lower() for field in fields(ScannerCandidate)}
    snapshot_fields = {field.name.lower() for field in fields(ScannerSnapshot)}
    assert not (candidate_fields & _DIRECTIONAL)
    assert not (snapshot_fields & _DIRECTIONAL)


# Y — provider neutrality: the scanner module imports no provider/transport/persistence.
def test_scanner_is_provider_neutral() -> None:
    source = inspect.getsource(scanner_module).lower()
    for forbidden in ("dhan", "httpx", "websocket", "sqlalchemy", "redis", "security_id"):
        assert forbidden not in source
