"""Unit tests for the per-(instrument, strategy) readiness engine (DECOUPLING PHASE G).

Covers every status, deterministic precedence, fail-closed invariants, per-strategy distinction,
the StrategyRequirements projection (incl. unsupported fail-closed), adjusted-unavailable, and
diagnostics — all against in-memory fakes with an injected ManualClock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_engine.clock import ManualClock
from app.market_history import AdjustedDataUnavailableError, PriceAdjustment, compute_coverage
from app.market_history.coverage import HistoricalCoverage
from app.strategies.enums import CandleCompleteness, FactNeed, StrategyTrigger
from app.strategies.requirements import StrategyRequirements
from app.strategy_readiness import (
    HistoricalNeed,
    ReadinessRequirement,
    StrategyReadinessEngine,
    StrategyReadinessStatus,
    UnsupportedReadinessRequirementError,
    readiness_requirement_from,
)

_NOW = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
_TD = date(2026, 9, 14)


def _dates(n: int) -> tuple[date, ...]:
    return tuple(date(2026, 6, 1) + timedelta(days=i) for i in range(n))


class _FakeUniverse:
    def __init__(self, members: set[str], version: int = 10) -> None:
        self._members = members
        self._version = version

    def is_member(self, instrument_identity: str) -> bool:
        return instrument_identity in self._members

    def current_universe_version(self) -> int:
        return self._version


class _FakeCoverage:
    def __init__(self) -> None:
        self.required: dict[str, int] = {}  # identity -> required trading days
        self.available: dict[str, int] = {}  # identity -> available of the required
        self.has_prev: set[str] = set()
        self.adjusted_unavailable = True

    def coverage(
        self, instrument_identity: str, *, trading_days: int, adjustment: object, as_of: date
    ) -> HistoricalCoverage:
        if adjustment is PriceAdjustment.ADJUSTED and self.adjusted_unavailable:
            raise AdjustedDataUnavailableError("no corporate-action authority")
        required = _dates(trading_days)
        stored = required[: self.available.get(instrument_identity, 0)]
        return compute_coverage(
            instrument_identity=instrument_identity, required_dates=required, stored_dates=stored
        )

    def has_previous_trading_bar(self, instrument_identity: str, *, as_of: date) -> bool:
        return instrument_identity in self.has_prev


class _FakeLive:
    def __init__(self) -> None:
        self.prev_close: dict[str, Decimal] = {}
        self.sess_open: dict[str, Decimal] = {}
        self.last: dict[str, Decimal] = {}
        self.observed: dict[str, datetime] = {}
        self.data_version: dict[str, int] = {}

    def previous_close(self, i: str) -> Decimal | None:
        return self.prev_close.get(i)

    def session_open(self, i: str) -> Decimal | None:
        return self.sess_open.get(i)

    def last_price(self, i: str) -> Decimal | None:
        return self.last.get(i)

    def observed_at(self, i: str) -> datetime | None:
        return self.observed.get(i)

    def data_universe_version(self, i: str) -> int | None:
        return self.data_version.get(i)


def _engine(
    universe: _FakeUniverse, coverage: _FakeCoverage, live: _FakeLive
) -> StrategyReadinessEngine:
    return StrategyReadinessEngine(
        universe=universe, coverage=coverage, live=live, clock=ManualClock(_NOW)
    )


def _evaluate(status_setup, requirement: ReadinessRequirement, identity: str = "NSE:TCS"):
    universe, coverage, live = status_setup
    engine = _engine(universe, coverage, live)
    return engine.evaluate(identity, "strat", requirement, trading_date=_TD)


def _healthy() -> tuple[_FakeUniverse, _FakeCoverage, _FakeLive]:
    universe = _FakeUniverse({"NSE:TCS"}, version=10)
    coverage = _FakeCoverage()
    coverage.required["NSE:TCS"] = 20
    coverage.available["NSE:TCS"] = 20
    coverage.has_prev.add("NSE:TCS")
    live = _FakeLive()
    live.prev_close["NSE:TCS"] = Decimal("100")
    live.sess_open["NSE:TCS"] = Decimal("101")
    live.last["NSE:TCS"] = Decimal("102")
    live.observed["NSE:TCS"] = _NOW
    return universe, coverage, live


_FULL = ReadinessRequirement(
    historical=HistoricalNeed(trading_days=20),
    needs_previous_day_ohlc=True,
    needs_previous_close=True,
    needs_session_open=True,
    needs_last_price=True,
    max_live_age=timedelta(minutes=5),
)


# --------------------------------------------------------------------------- #
# READY + fail-closed statuses
# --------------------------------------------------------------------------- #
def test_ready_happy_path() -> None:
    assert _evaluate(_healthy(), _FULL).status is StrategyReadinessStatus.READY


def test_removed_when_not_in_universe() -> None:
    setup = _healthy()
    setup[0]._members.clear()
    assert _evaluate(setup, _FULL).status is StrategyReadinessStatus.REMOVED


def test_universe_mismatch() -> None:
    setup = _healthy()
    setup[2].data_version["NSE:TCS"] = 11  # data at v11, backend at v10
    result = _evaluate(setup, _FULL)
    assert result.status is StrategyReadinessStatus.UNIVERSE_MISMATCH
    assert result.reasons[0].required == "10" and result.reasons[0].available == "11"


def test_missing_history_partial() -> None:
    setup = _healthy()
    setup[1].available["NSE:TCS"] = 19  # 19/20
    result = _evaluate(setup, _FULL)
    assert result.status is StrategyReadinessStatus.MISSING_HISTORY
    assert result.reasons[0].required == "20" and result.reasons[0].available == "19"


def test_missing_history_previous_day() -> None:
    setup = _healthy()
    setup[1].has_prev.discard("NSE:TCS")
    req = ReadinessRequirement(needs_previous_day_ohlc=True)
    assert _evaluate(setup, req).status is StrategyReadinessStatus.MISSING_HISTORY


def test_missing_reference() -> None:
    setup = _healthy()
    del setup[2].prev_close["NSE:TCS"]
    assert _evaluate(setup, _FULL).status is StrategyReadinessStatus.MISSING_REFERENCE


def test_missing_live_data_session_open() -> None:
    setup = _healthy()
    del setup[2].sess_open["NSE:TCS"]
    result = _evaluate(setup, _FULL)
    assert result.status is StrategyReadinessStatus.MISSING_LIVE_DATA
    assert result.reasons[0].field == "session_open"


def test_missing_live_data_last_price() -> None:
    setup = _healthy()
    del setup[2].last["NSE:TCS"]
    assert _evaluate(setup, _FULL).status is StrategyReadinessStatus.MISSING_LIVE_DATA


def test_stale_live_data() -> None:
    setup = _healthy()
    setup[2].observed["NSE:TCS"] = _NOW - timedelta(minutes=10)  # older than 5m max
    assert _evaluate(setup, _FULL).status is StrategyReadinessStatus.STALE


def test_warming_up_before_first_observation() -> None:
    setup = _healthy()
    del setup[2].observed["NSE:TCS"]  # never observed
    result = _evaluate(setup, _FULL)
    assert result.status is StrategyReadinessStatus.WARMING_UP
    assert result.reasons[0].code.value == "awaiting_first_observation"


# --------------------------------------------------------------------------- #
# precedence + per-strategy distinction
# --------------------------------------------------------------------------- #
def test_precedence_removed_beats_everything() -> None:
    setup = _healthy()
    setup[0]._members.clear()
    setup[1].available["NSE:TCS"] = 0  # also missing history
    setup[2].data_version["NSE:TCS"] = 99  # also mismatch
    assert _evaluate(setup, _FULL).status is StrategyReadinessStatus.REMOVED


def test_history_beats_reference_and_live() -> None:
    setup = _healthy()
    setup[1].available["NSE:TCS"] = 5
    del setup[2].prev_close["NSE:TCS"]  # also missing reference
    del setup[2].sess_open["NSE:TCS"]  # also missing live
    assert _evaluate(setup, _FULL).status is StrategyReadinessStatus.MISSING_HISTORY


def test_new_fno_member_8_of_20_per_strategy() -> None:
    setup = _healthy()
    setup[1].available["NSE:TCS"] = 8  # only 8 of 20 historical days
    # strategy requiring 20 days -> MISSING_HISTORY
    twenty = ReadinessRequirement(historical=HistoricalNeed(trading_days=20))
    assert _evaluate(setup, twenty).status is StrategyReadinessStatus.MISSING_HISTORY
    # strategy requiring only previous-day OHLC (available) -> READY (no live requirement)
    prev_only = ReadinessRequirement(needs_previous_day_ohlc=True)
    assert _evaluate(setup, prev_only).status is StrategyReadinessStatus.READY


# --------------------------------------------------------------------------- #
# adjusted unavailable + unknown requirement (fail closed)
# --------------------------------------------------------------------------- #
def test_adjusted_history_unavailable_is_missing_history() -> None:
    setup = _healthy()
    req = ReadinessRequirement(
        historical=HistoricalNeed(trading_days=20, adjustment=PriceAdjustment.ADJUSTED)
    )
    result = _evaluate(setup, req)
    assert result.status is StrategyReadinessStatus.MISSING_HISTORY
    assert result.reasons[0].code.value == "adjusted_history_unavailable"


def test_projection_supported_fact_needs() -> None:
    reqs = StrategyRequirements(
        fact_needs=(FactNeed.PREVIOUS_SESSION, FactNeed.SESSION, FactNeed.LATEST_TICK),
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY,
    )
    projected = readiness_requirement_from(reqs)
    assert projected.needs_previous_day_ohlc and projected.needs_session_open
    assert projected.needs_last_price


def test_projection_fails_closed_on_unsupported_fact_need() -> None:
    reqs = StrategyRequirements(
        fact_needs=(FactNeed.LATEST_QUOTE,),  # not projectable into planning readiness
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY,
    )
    with pytest.raises(UnsupportedReadinessRequirementError):
        readiness_requirement_from(reqs)


# --------------------------------------------------------------------------- #
# batch + snapshot + diagnostics + determinism
# --------------------------------------------------------------------------- #
def test_batch_snapshot_and_totals() -> None:
    universe = _FakeUniverse({"NSE:A", "NSE:B"}, version=10)
    coverage = _FakeCoverage()
    coverage.available["NSE:A"] = 20
    coverage.available["NSE:B"] = 5
    live = _FakeLive()
    engine = _engine(universe, coverage, live)
    reqs = {"s20": ReadinessRequirement(historical=HistoricalNeed(trading_days=20))}
    snapshot = engine.evaluate_batch(["NSE:A", "NSE:B", "NSE:GONE"], reqs, trading_date=_TD)
    assert len(snapshot.results) == 3
    totals = snapshot.totals_by_status
    assert totals[StrategyReadinessStatus.READY] == 1  # A complete
    assert totals[StrategyReadinessStatus.MISSING_HISTORY] == 1  # B 5/20
    assert totals[StrategyReadinessStatus.REMOVED] == 1  # GONE
    assert engine.diagnostics().evaluations_total == 3


def test_determinism_same_inputs_same_result() -> None:
    first = _evaluate(_healthy(), _FULL)
    second = _evaluate(_healthy(), _FULL)
    assert first.status is second.status
    assert first.evaluated_at == second.evaluated_at == _NOW


def test_result_has_no_credentials() -> None:
    dumped = _evaluate(_healthy(), _FULL).model_dump_json().lower()
    for secret in ("token", "totp", "password", "authorization", "secret"):
        assert secret not in dumped
