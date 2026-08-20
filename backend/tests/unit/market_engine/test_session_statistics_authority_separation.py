"""Per-source session-statistics authority separation (P4.6E6A; ADR-009 D6/D7).

Proves the two canonical source classes — the staged :class:`SessionStatisticsObservation`
(REST-backed, ADR-009) and the tick-carried :attr:`Tick.session_ohlc` aggregate (ADR-008) —
are gated by independent capability bits, and that an unverified source can neither
establish nor mutate an authoritative snapshot owned by a verified source. Both production
bits remain disabled; verification here uses test-only capability fixtures.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import (
    MarketContext,
    MarketState,
    SessionContext,
    SessionStatistics,
    SessionStatisticsQuality,
)
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.session_statistics import (
    SessionStatisticsAuthority,
    apply_session_ohlc,
    resolve_session_statistics,
    update_session_statistics,
)
from app.market_engine.state import InstrumentState, InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.schemas.market_data import (
    Instrument,
    ProviderSessionOhlc,
    SessionStatisticsObservation,
    Tick,
)
from app.strategies.configuration import StrategyConfiguration
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import (
    CandleCompleteness,
    EmissionPolicy,
    FactNeed,
    StrategyCategory,
    StrategyTrigger,
)
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements
from app.strategy_manager.readiness import assess_readiness
from app.strategy_manager.records import Readiness

_DATE = date(2026, 8, 6)
_NEXT_DATE = date(2026, 8, 7)
_T0 = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST — LIVE session
_T5 = _T0 + timedelta(seconds=5)
_T11 = _T0 + timedelta(seconds=11)
_LIVE2 = _T0 + timedelta(minutes=1)
_LIVE3 = _T0 + timedelta(minutes=2)
_PRE_OPEN = datetime(2026, 8, 6, 3, 32, tzinfo=UTC)  # 09:02 IST
_NOW = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)  # engine clock after every event
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)

_NONE = SessionStatisticsAuthority()
_OBS_ONLY = SessionStatisticsAuthority(staged_observation_verified=True)
_TICK_ONLY = SessionStatisticsAuthority(tick_aggregate_verified=True)
_BOTH = SessionStatisticsAuthority(staged_observation_verified=True, tick_aggregate_verified=True)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _ohlc(
    *, open_: str = "100", high: str = "105", low: str = "98", close: str = "101"
) -> ProviderSessionOhlc:
    return ProviderSessionOhlc(
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
    )


def _obs(
    *,
    symbol: str = "RELIANCE",
    trading_date: date = _DATE,
    observed_at: datetime = _T0,
    open_: str = "100",
    high: str = "105",
    low: str = "98",
    close: str = "101",
) -> SessionStatisticsObservation:
    return SessionStatisticsObservation(
        instrument=_instrument(symbol),
        trading_date=trading_date,
        observed_at=observed_at,
        session_ohlc=_ohlc(open_=open_, high=high, low=low, close=close),
    )


def _tick(
    symbol: str = "RELIANCE",
    *,
    at: datetime = _T0,
    open_: str = "100",
    high: str = "105",
    low: str = "98",
    with_ohlc: bool = True,
) -> Tick:
    ohlc = _ohlc(open_=open_, high=high, low=low) if with_ohlc else None
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal("100"),
        traded_quantity=1,
        session_ohlc=ohlc,
    )


def _session(state: MarketState = MarketState.LIVE_SESSION, day: date = _DATE) -> SessionContext:
    return SessionContext(trading_date=day, market_state=state, exchange_timezone="Asia/Kolkata")


def _authoritative(stats: SessionStatistics | None) -> bool:
    return stats is not None and stats.quality is SessionStatisticsQuality.AUTHORITATIVE


# --------------------------------------------------------------------------- #
# §27 Authority model
# --------------------------------------------------------------------------- #
def test_default_authority_disables_both_sources() -> None:
    authority = SessionStatisticsAuthority()
    assert authority.staged_observation_verified is False
    assert authority.tick_aggregate_verified is False


def test_authority_is_immutable() -> None:
    authority = SessionStatisticsAuthority()
    with pytest.raises(dataclasses.FrozenInstanceError):
        authority.tick_aggregate_verified = True  # type: ignore[misc]


def test_observation_only_leaves_tick_disabled() -> None:
    assert _OBS_ONLY.staged_observation_verified is True
    assert _OBS_ONLY.tick_aggregate_verified is False


def test_tick_only_leaves_observation_disabled() -> None:
    assert _TICK_ONLY.tick_aggregate_verified is True
    assert _TICK_ONLY.staged_observation_verified is False


def test_both_sources_can_be_enabled_independently() -> None:
    assert _BOTH.staged_observation_verified is True
    assert _BOTH.tick_aggregate_verified is True


def test_authority_exposes_only_source_class_fields() -> None:
    assert set(SessionStatisticsAuthority.__dataclass_fields__) == {
        "staged_observation_verified",
        "tick_aggregate_verified",
    }


def test_old_ambiguous_provider_field_is_removed() -> None:
    with pytest.raises(TypeError):
        SessionStatisticsAuthority(provider_aggregate_verified=True)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# §28 Observation authority path
# --------------------------------------------------------------------------- #
def _resolve_observation(
    observation: SessionStatisticsObservation | None,
    *,
    authority: SessionStatisticsAuthority,
    session: SessionContext,
    previous: SessionStatistics | None = None,
) -> tuple[SessionStatistics | None, SessionStatisticsObservation | None]:
    return resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_T0,
        staged=observation,
        session=session,
        previous=previous,
        authority=authority,
    )


def test_unverified_observation_cannot_establish_authority() -> None:
    stats, staged = _resolve_observation(_obs(), authority=_NONE, session=_session())
    assert stats is None and staged is None  # examined and consumed, but not promoted


def test_verified_observation_establishes_authority() -> None:
    stats, staged = _resolve_observation(_obs(high="105"), authority=_OBS_ONLY, session=_session())
    assert _authoritative(stats) and staged is None
    assert stats is not None and stats.high_price == Decimal("105")


def test_tick_authority_does_not_authorize_the_observation() -> None:
    stats, _ = _resolve_observation(_obs(), authority=_TICK_ONLY, session=_session())
    assert stats is None  # the tick bit never crosses over to the observation source


def test_newer_verified_observation_progresses() -> None:
    first, _ = _resolve_observation(
        _obs(observed_at=_T0, high="105", low="98"), authority=_OBS_ONLY, session=_session()
    )
    second, _ = _resolve_observation(
        _obs(observed_at=_LIVE2, high="108", low="95"),
        authority=_OBS_ONLY,
        session=_session(),
        previous=first,
    )
    assert second is not None
    assert second.high_price == Decimal("108") and second.low_price == Decimal("95")


def test_stale_verified_observation_is_rejected() -> None:
    prior, _ = _resolve_observation(
        _obs(observed_at=_LIVE2, high="108", low="95"), authority=_OBS_ONLY, session=_session()
    )
    result, _ = _resolve_observation(
        _obs(observed_at=_T0, high="120", low="90"),
        authority=_OBS_ONLY,
        session=_session(),
        previous=prior,
    )
    assert result is prior  # stale as_of never regresses fresher state


def test_wrong_trading_date_observation_is_dropped() -> None:
    stats, staged = resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_T0,
        staged=_obs(trading_date=_DATE, observed_at=_T0),
        session=_session(day=_NEXT_DATE),
        previous=None,
        authority=_OBS_ONLY,
    )
    assert stats is None and staged is None  # prior-day observation dropped, no leakage


def test_pre_open_observation_is_not_authoritative() -> None:
    kept = _obs(observed_at=_T0)
    stats, staged = _resolve_observation(
        kept, authority=_OBS_ONLY, session=_session(MarketState.PRE_OPEN)
    )
    assert stats is None and staged == kept  # retained pending until LIVE_SESSION


# --------------------------------------------------------------------------- #
# §29 Tick authority path
# --------------------------------------------------------------------------- #
def _update(
    tick: Tick,
    *,
    authority: SessionStatisticsAuthority,
    previous: SessionStatistics | None = None,
    session: SessionContext | None = None,
) -> SessionStatistics | None:
    return update_session_statistics(
        tick=tick,
        session=session if session is not None else _session(),
        previous=previous,
        authority=authority,
    )


def test_unverified_tick_aggregate_cannot_establish_authority() -> None:
    assert _update(_tick(), authority=_NONE) is None


def test_verified_tick_aggregate_establishes_authority() -> None:
    stats = _update(_tick(high="105"), authority=_TICK_ONLY)
    assert _authoritative(stats) and stats is not None and stats.high_price == Decimal("105")


def test_observation_authority_does_not_authorize_the_tick() -> None:
    assert _update(_tick(), authority=_OBS_ONLY) is None  # the observation bit never crosses over


def test_newer_verified_tick_aggregate_progresses() -> None:
    first = _update(_tick(at=_T0, high="105", low="98"), authority=_TICK_ONLY)
    second = _update(_tick(at=_LIVE2, high="108", low="95"), authority=_TICK_ONLY, previous=first)
    assert second is not None and second.high_price == Decimal("108")


def test_stale_verified_tick_cannot_regress() -> None:
    prior = _update(_tick(at=_LIVE2, high="108", low="95"), authority=_TICK_ONLY)
    result = _update(_tick(at=_T0, high="120", low="90"), authority=_TICK_ONLY, previous=prior)
    assert result is prior


def test_verified_tick_open_change_fails_closed() -> None:
    prior = _update(_tick(at=_T0, open_="100", high="105", low="98"), authority=_TICK_ONLY)
    result = _update(
        _tick(at=_LIVE2, open_="101", high="108", low="97"), authority=_TICK_ONLY, previous=prior
    )
    assert result is prior  # corrected open never merges — whole snapshot rejected


# --------------------------------------------------------------------------- #
# §30 Cross-source safety
# --------------------------------------------------------------------------- #
def _establish_from_observation(
    authority: SessionStatisticsAuthority,
) -> SessionStatistics:
    stats, _ = _resolve_observation(
        _obs(observed_at=_T0, open_="100", high="105", low="98"),
        authority=authority,
        session=_session(),
    )
    assert stats is not None
    return stats


def _apply_unverified_tick(
    previous: SessionStatistics, *, high: str, low: str, at: datetime = _LIVE2
) -> SessionStatistics | None:
    # authority verifies the observation source only; the tick source stays unverified.
    stats, staged = resolve_session_statistics(
        aggregate=_ohlc(high=high, low=low),
        aggregate_as_of=at,
        staged=None,
        session=_session(),
        previous=previous,
        authority=_OBS_ONLY,
    )
    assert staged is None
    return stats


def test_verified_observation_then_unverified_tick_leaves_stats_unchanged() -> None:
    prior = _establish_from_observation(_OBS_ONLY)
    assert _apply_unverified_tick(prior, high="105", low="98") is prior


def test_unverified_tick_higher_high_is_ignored() -> None:
    prior = _establish_from_observation(_OBS_ONLY)
    result = _apply_unverified_tick(prior, high="200", low="98")
    assert result is prior and result.high_price == Decimal("105")


def test_unverified_tick_lower_low_is_ignored() -> None:
    prior = _establish_from_observation(_OBS_ONLY)
    result = _apply_unverified_tick(prior, high="105", low="50")
    assert result is prior and result.low_price == Decimal("98")


def test_unverified_tick_does_not_refresh_as_of() -> None:
    prior = _establish_from_observation(_OBS_ONLY)
    result = _apply_unverified_tick(prior, high="200", low="50", at=_LIVE3)
    assert result is not None and result.as_of == _T0  # the verified observation's instant


def test_verified_tick_then_unverified_observation_leaves_stats_unchanged() -> None:
    prior = _update(_tick(at=_T0, high="105", low="98"), authority=_TICK_ONLY)
    assert prior is not None
    stats, staged = resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_LIVE2,
        staged=_obs(observed_at=_LIVE2, high="200", low="50"),
        session=_session(),
        previous=prior,
        authority=_TICK_ONLY,  # observation source unverified
    )
    assert stats is prior and staged is None  # examined/consumed but cannot mutate


def test_whole_snapshot_rule_no_field_merge_across_sources() -> None:
    prior = _establish_from_observation(_OBS_ONLY)  # open 100, high 105, low 98
    # A verified newer observation with a corrected open is rejected wholesale — the engine
    # never grafts the new high onto the old open.
    result, _ = resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_LIVE2,
        staged=_obs(observed_at=_LIVE2, open_="101", high="108", low="97"),
        session=_session(),
        previous=prior,
        authority=_OBS_ONLY,
    )
    assert result is prior  # not a (open=100, high=108) hybrid


def test_staged_observation_precedence_is_independent_of_authority() -> None:
    # With BOTH sources verified, an eligible staged observation still wins the cycle.
    stats, staged = resolve_session_statistics(
        aggregate=_ohlc(high="200"),  # the tick-carried aggregate
        aggregate_as_of=_T0,
        staged=_obs(observed_at=_T0, high="105"),  # the observation
        session=_session(),
        previous=None,
        authority=_BOTH,
    )
    assert staged is None and stats is not None
    assert stats.high_price == Decimal("105")  # observation, not the tick's 200


def test_cross_source_sequence_is_deterministic() -> None:
    def run() -> list[SessionStatistics | None]:
        results: list[SessionStatistics | None] = []
        prior = _establish_from_observation(_OBS_ONLY)
        results.append(prior)
        results.append(_apply_unverified_tick(prior, high="200", low="50"))
        return results

    assert run() == run()


# --------------------------------------------------------------------------- #
# §15 Cross-capability matrix (which source may establish under each combination)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("authority", "observation_authoritative", "tick_authoritative"),
    [
        (_NONE, False, False),
        (_OBS_ONLY, True, False),
        (_TICK_ONLY, False, True),
        (_BOTH, True, True),
    ],
)
def test_cross_capability_matrix(
    authority: SessionStatisticsAuthority,
    observation_authoritative: bool,
    tick_authoritative: bool,
) -> None:
    observation_stats = apply_session_ohlc(
        aggregate=_ohlc(),
        aggregate_as_of=_T0,
        session=_session(),
        previous=None,
        source_verified=authority.staged_observation_verified,
    )
    tick_stats = apply_session_ohlc(
        aggregate=_ohlc(),
        aggregate_as_of=_T0,
        session=_session(),
        previous=None,
        source_verified=authority.tick_aggregate_verified,
    )
    assert _authoritative(observation_stats) is observation_authoritative
    assert _authoritative(tick_stats) is tick_authoritative


# --------------------------------------------------------------------------- #
# Engine wiring: freshness, versioning, isolation, replay
# --------------------------------------------------------------------------- #
def _classifier() -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(), exchange_timezone="Asia/Kolkata"
    )


def _engine(
    *,
    authority: SessionStatisticsAuthority,
    symbols: tuple[str, ...] = ("RELIANCE",),
) -> tuple[TickEngine, InstrumentStateRegistry, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(s) for s in symbols)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
        session=_classifier(),
        session_statistics_authority=authority,
    )
    return engine, registry, recorded


def _state(registry: InstrumentStateRegistry, symbol: str = "RELIANCE") -> InstrumentState:
    state = registry.get(_instrument(symbol))
    assert state is not None
    return state


# §31 Freshness safety — an unverified tick must not extend a verified snapshot's freshness
def _readiness(stats: SessionStatistics, *, observed_at: datetime) -> Readiness:
    context = MarketContext.initial(
        _instrument(),
        sequence=1,
        event_timestamp=observed_at,
        observed_at=observed_at,
        session=_session(),
        session_statistics=stats,
    )
    requirements = StrategyRequirements(
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.PARTIAL_ALLOWED,
        fact_needs=(FactNeed.SESSION_STATISTICS,),
        freshness=(
            FactFreshnessRequirement(
                fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=10)
            ),
        ),
    )
    return assess_readiness(
        descriptor=StrategyDescriptor(
            strategy_id="alpha",
            display_name="Alpha",
            description="A test strategy.",
            version="1.0.0",
            category=StrategyCategory.OPENING_SESSION,
            emission_policy=EmissionPolicy.EDGE_TRIGGERED,
        ),
        requirements=requirements,
        configuration=StrategyConfiguration(config_version="1.0.0"),
        configuration_type=StrategyConfiguration,
        context=context,
    )


def test_unverified_tick_does_not_extend_verified_freshness() -> None:
    # Verified observation at T0, then an unverified tick at T5 with a higher high.
    established = _establish_from_observation(_OBS_ONLY)
    after_tick = _apply_unverified_tick(established, high="200", low="50", at=_T5)
    assert after_tick is not None and after_tick.as_of == _T0  # as_of not refreshed to T5
    # At T11 with max_age 10s the snapshot is stale — freshness is measured from T0 (age 11s),
    # not from the unverified tick at T5 (which would give age 6s and READY).
    assert _readiness(after_tick, observed_at=_T11) is Readiness.SESSION_STATISTICS_STALE
    # Positive control: measured from the un-refreshed T0, an 8s-old snapshot is still fresh.
    assert _readiness(after_tick, observed_at=_T0 + timedelta(seconds=8)) is Readiness.READY


# §32 Version/event discipline
def test_staging_under_separation_mints_no_version() -> None:
    _engine_obj, registry, recorded = _engine(authority=_BOTH)
    registry.stage_session_statistics_observation(_instrument(), _obs())
    assert recorded == []  # staging alone mints no MarketContext version/event


def test_stage_plus_next_datum_is_exactly_one_version() -> None:
    engine, registry, recorded = _engine(authority=_OBS_ONLY)
    registry.stage_session_statistics_observation(_instrument(), _obs(high="105"))
    result = engine.process(_tick(at=_T0, with_ohlc=False))
    assert result.context is not None and result.context.version == 1
    assert [type(e) for e in recorded] == [MarketContextCreated]


def test_prior_version_is_immutable_after_unverified_tick() -> None:
    engine, registry, _ = _engine(authority=_OBS_ONLY)
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_T0, high="105"))
    v1 = engine.process(_tick(at=_T0, with_ohlc=False)).context
    engine.process(_tick(at=_LIVE2, high="200", low="50"))  # unverified tick aggregate
    assert v1 is not None and v1.session_statistics is not None
    assert v1.session_statistics.high_price == Decimal("105")  # v1 unchanged


def test_unverified_tick_adds_no_extra_statistics_version() -> None:
    engine, registry, recorded = _engine(authority=_OBS_ONLY)
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_T0, high="105"))
    engine.process(_tick(at=_T0, with_ohlc=False))  # v1 — observation surfaces
    engine.process(_tick(at=_LIVE2, high="200", low="50"))  # v2 — ordinary accepted tick
    # Exactly two versions: each from an accepted datum; the unverified statistics
    # update contributed no extra statistics-only version.
    assert [type(e) for e in recorded] == [MarketContextCreated, MarketContextUpdated]
    stats = _state(registry).session_statistics
    assert stats is not None and stats.high_price == Decimal("105")  # unchanged by the tick


# §33 Multi-instrument isolation
def test_observation_authority_does_not_leak_to_another_instruments_tick() -> None:
    engine, registry, _ = _engine(authority=_OBS_ONLY, symbols=("RELIANCE", "TCS"))
    registry.stage_session_statistics_observation(
        _instrument("RELIANCE"), _obs(symbol="RELIANCE", high="105")
    )
    engine.process(_tick("RELIANCE", at=_T0, with_ohlc=False))
    engine.process(_tick("TCS", at=_T0, high="105", low="98"))  # tick aggregate, unverified
    assert _authoritative(_state(registry, "RELIANCE").session_statistics)
    assert _state(registry, "TCS").session_statistics is None


def test_tick_authority_does_not_leak_to_another_instruments_observation() -> None:
    engine, registry, _ = _engine(authority=_TICK_ONLY, symbols=("RELIANCE", "TCS"))
    registry.stage_session_statistics_observation(
        _instrument("TCS"), _obs(symbol="TCS", open_="200", high="210", low="195", close="205")
    )
    engine.process(_tick("RELIANCE", at=_T0, high="105", low="98"))  # verified tick aggregate
    engine.process(_tick("TCS", at=_T0, with_ohlc=False))  # surfaces the unverified observation
    assert _authoritative(_state(registry, "RELIANCE").session_statistics)
    assert _state(registry, "TCS").session_statistics is None


# §34 Replay determinism through the engine
def test_engine_replay_is_deterministic_under_separation() -> None:
    def run() -> list[object]:
        engine, registry, _ = _engine(authority=_OBS_ONLY)
        results: list[object] = []
        for at, high in ((_T0, "105"), (_LIVE2, "108"), (_LIVE3, "108")):
            registry.stage_session_statistics_observation(
                _instrument(), _obs(observed_at=at, high=high, low="97")
            )
            context = engine.process(_tick(at=at, with_ohlc=False)).context
            assert context is not None
            results.append(context.session_statistics)
        return results

    assert run() == run()
