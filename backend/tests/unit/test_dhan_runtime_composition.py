"""Dhan runtime composition boundary & provider/universe resolution (RUN-B; ADR-010/004).

Proves provider-enabled/disabled composition, the fail-closed canonical universe
reduction, provider lifecycle ordering and cleanup, and that no Dhan-specific type
crosses into the broker-neutral LiveMarketRuntime. Uses provider test doubles — no
network, no real credentials.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from app.adapters.base.broker_adapter import BrokerAdapter
from app.adapters.base.provider_coordinator import ProviderInitializationError
from app.adapters.dhan.adapter import DhanRestAdapter
from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.market_engine.calendar_data import (
    TradingCalendarDataset,
    load_nse_cm_2026_dataset,
)
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketState
from app.market_engine.sequence import MonotonicSequence
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketData,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
)
from app.services.dhan_runtime_composition import (
    AuthoritativeCalendarUnavailableError,
    RuntimeComposition,
    UniverseResolutionError,
    _DeferredContinuitySink,
    compose_market_runtime,
)
from app.services.strategy_requirements_wiring import HistoricalWarmupAdapter

_NOW = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_ERROR_THRESHOLD = 3


def _disabled_settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


def _enabled_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "development",
        "database_url": _DB,
        "redis_url": _REDIS,
        "market_provider_enabled": True,
        "dhan_auth_mode": "totp",
        "dhan_client_id": "client-id",
        "dhan_pin": "123456",
        "dhan_totp_secret": "totp-secret",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _reference(symbol: str) -> DhanInstrumentReference:
    return DhanInstrumentReference(
        instrument=_instrument(symbol),
        security_id=f"SEC-{symbol}",
        underlying_security_id=None,
        exchange_segment="NSE_EQ",
        provider_instrument_type="ES",
    )


def _universe(symbols: tuple[str, ...]) -> DhanCashEquityLiveUniverse:
    return DhanCashEquityLiveUniverse(
        underlyings=(),
        cash_references=tuple(_reference(s) for s in symbols),
        missing_underlyings=(),
        ambiguous_underlyings=(),
        symbol_mismatches=(),
    )


class _FakeAdapter(BrokerAdapter):
    """A recording provider double satisfying the LiveUniverseAdapter contract."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        universe: DhanCashEquityLiveUniverse,
        health: ProviderStatus = ProviderStatus.HEALTHY,
        fail_on: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._universe = universe
        self._health = health
        self._fail_on = fail_on
        self._stream_gate = asyncio.Event()

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        # A live-feed double: yields nothing and blocks until cancelled on shutdown.
        self.calls.append("stream")
        for event in ():
            yield event
        await self._stream_gate.wait()

    def _maybe_fail(self, name: str) -> None:
        if name == self._fail_on:
            raise RuntimeError(f"injected failure in {name}")

    async def connect(self) -> None:
        self.calls.append("connect")
        self._maybe_fail("connect")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")

    async def get_health(self) -> ProviderHealth:
        self.calls.append("get_health")
        self._maybe_fail("get_health")
        return ProviderHealth(status=self._health, observed_at=_NOW)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        self.calls.append("load_instruments")
        self._maybe_fail("load_instruments")
        return tuple(ref.instrument for ref in self._universe.cash_references)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        self.calls.append("load_universe")
        self._maybe_fail("load_universe")
        return self._universe


async def _compose(adapter: _FakeAdapter, settings: Settings | None = None) -> RuntimeComposition:
    return await compose_market_runtime(
        settings=settings if settings is not None else _enabled_settings(),
        error_threshold=_ERROR_THRESHOLD,
        adapter=adapter,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )


# --------------------------------------------------------------------------- #
# Settings: explicit provider-enabled configuration & credential policy
# --------------------------------------------------------------------------- #
def test_disabled_provider_needs_no_credentials() -> None:
    assert _disabled_settings().market_provider_enabled is False


def test_enabled_provider_with_complete_credentials_is_valid() -> None:
    assert _enabled_settings().market_provider_enabled is True


def test_enabled_provider_missing_credentials_fails() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="development",
            database_url=_DB,
            redis_url=_REDIS,
            market_provider_enabled=True,  # totp mode, no client_id/pin/totp
        )


def test_enabled_access_token_mode_requires_token() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="development",
            database_url=_DB,
            redis_url=_REDIS,
            market_provider_enabled=True,
            dhan_auth_mode="access_token",  # no access token
        )


# --------------------------------------------------------------------------- #
# Disabled mode
# --------------------------------------------------------------------------- #
async def test_enabled_default_loads_packaged_dataset_and_wires_coordinator() -> None:
    # With no injected dataset the packaged NSE 2026 dataset is loaded, and the
    # fact/live seams + session-statistics refresh + historical warmup all compose.
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)  # no calendar_dataset → packaged load
    assert composition.runtime.requirements_coordinator is not None
    await composition.shutdown()


async def test_enabled_with_dataset_wires_a_real_requirements_coordinator() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await compose_market_runtime(
        settings=_enabled_settings(),
        error_threshold=_ERROR_THRESHOLD,
        adapter=adapter,
        calendar_dataset=load_nse_cm_2026_dataset(),
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )
    assert composition.runtime.requirements_coordinator is not None
    await composition.shutdown()


async def test_provisioned_dataset_composes_a_real_warmup_service() -> None:
    # §14 A: a validated dataset composes a real HistoricalWarmupService port, not the
    # fail-closed UnavailableHistoricalWarmup.
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    coordinator = composition.runtime.requirements_coordinator
    assert coordinator is not None
    assert isinstance(coordinator._warmup, HistoricalWarmupAdapter)  # noqa: SLF001
    await composition.shutdown()


# --------------------------------------------------------------------------- #
# Authoritative-dataset load failure — fail-fast (ADR-011 dataset-failure-policy)
# --------------------------------------------------------------------------- #
_LOADER = "app.services.dhan_runtime_composition.load_nse_cm_2026_dataset"


def _fail_load(monkeypatch: pytest.MonkeyPatch, factory: Callable[[], object]) -> None:
    monkeypatch.setattr(_LOADER, factory)


async def _expect_calendar_failure(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], object],
    settings: Settings | None = None,
) -> tuple[AuthoritativeCalendarUnavailableError, _FakeAdapter]:
    """Compose enabled with a failing loader and return the governed error + the adapter."""
    _fail_load(monkeypatch, factory)
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    with pytest.raises(AuthoritativeCalendarUnavailableError) as exc_info:
        await _compose(adapter, settings)
    return exc_info.value, adapter


def _invalid_dataset_loader(payload: dict[str, object]) -> Callable[[], object]:
    """A loader replacement that validates a rule-violating payload (raises ValidationError)."""

    def _factory() -> object:
        return TradingCalendarDataset.model_validate_json(json.dumps(payload))

    return _factory


def _packaged_payload() -> dict[str, object]:
    return load_nse_cm_2026_dataset().model_dump(mode="json")


async def test_missing_dataset_resource_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 B/L/S/O/R: a missing resource fails fast; provider cleaned up once; cause preserved;
    # no ingestion/market processing; nse_holidays empty (default) cannot rescue (§13 J).
    def _boom() -> object:
        raise FileNotFoundError("packaged dataset missing")

    error, adapter = await _expect_calendar_failure(monkeypatch, _boom)
    assert isinstance(error.__cause__, FileNotFoundError)
    assert adapter.calls.count("disconnect") == 1
    assert "stream" not in adapter.calls


async def test_malformed_json_dataset_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 C: malformed JSON → governed error wrapping a ValidationError.
    def _boom() -> object:
        return TradingCalendarDataset.model_validate_json("not valid json")

    error, adapter = await _expect_calendar_failure(monkeypatch, _boom)
    assert isinstance(error.__cause__, ValidationError)
    assert adapter.calls.count("disconnect") == 1


async def test_mis_encoded_dataset_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 E: a mis-encoded resource (UnicodeDecodeError) → governed error.
    def _boom() -> object:
        return b"\xff\xfe".decode("utf-8")

    error, _ = await _expect_calendar_failure(monkeypatch, _boom)
    assert isinstance(error.__cause__, UnicodeDecodeError)


async def test_inverted_coverage_dataset_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 D/F: an inverted coverage window → ValidationError → governed error.
    payload = _packaged_payload()
    payload["coverage_start"], payload["coverage_end"] = (
        payload["coverage_end"],
        payload["coverage_start"],
    )
    error, _ = await _expect_calendar_failure(monkeypatch, _invalid_dataset_loader(payload))
    assert isinstance(error.__cause__, ValidationError)


async def test_open_closed_conflict_dataset_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 G: a date that is both OPEN and CLOSED → ValidationError → governed error.
    payload = _packaged_payload()
    open_sessions = payload["open_sessions"]
    assert isinstance(open_sessions, list)
    payload["closed_dates"] = [*payload["closed_dates"], open_sessions[0]]  # type: ignore[misc]
    error, _ = await _expect_calendar_failure(monkeypatch, _invalid_dataset_loader(payload))
    assert isinstance(error.__cause__, ValidationError)


async def test_invalid_interval_dataset_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 H: an inverted session interval → ValidationError → governed error.
    payload = _packaged_payload()
    overrides = payload["session_overrides"]
    assert isinstance(overrides, list)
    overrides[0]["intervals"][0] = {"start": "15:30", "end": "09:15"}
    error, _ = await _expect_calendar_failure(monkeypatch, _invalid_dataset_loader(payload))
    assert isinstance(error.__cause__, ValidationError)


async def test_populated_nse_holidays_cannot_rescue(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 I: a populated settings.nse_holidays never rescues a failed authoritative dataset.
    def _boom() -> object:
        raise FileNotFoundError("packaged dataset missing")

    settings = _enabled_settings(nse_holidays="2026-01-15,2026-01-26")
    _, adapter = await _expect_calendar_failure(monkeypatch, _boom, settings)
    assert adapter.calls.count("disconnect") == 1


async def test_calendar_monitor_enabled_cannot_rescue(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 K: the secondary monitor never rescues startup; failure still fails fast.
    def _boom() -> object:
        raise FileNotFoundError("packaged dataset missing")

    settings = _enabled_settings(calendar_monitor_enabled=True)
    _, adapter = await _expect_calendar_failure(monkeypatch, _boom, settings)
    assert adapter.calls.count("disconnect") == 1


async def test_provider_failure_is_not_a_calendar_error() -> None:
    # §13 T: provider connectivity failure is a distinct error type, never the calendar error.
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)), fail_on="connect")
    with pytest.raises(Exception) as exc_info:  # noqa: PT011  (asserting the negative below)
        await _compose(adapter)
    assert not isinstance(exc_info.value, AuthoritativeCalendarUnavailableError)


async def test_disabled_mode_never_resolves_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    # §13 U: disabled mode composes a dormant runtime and never touches the dataset loader.
    def _must_not_load() -> object:
        raise AssertionError("disabled mode must not resolve the calendar dataset")

    _fail_load(monkeypatch, _must_not_load)
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await compose_market_runtime(
        settings=_disabled_settings(),
        error_threshold=_ERROR_THRESHOLD,
        adapter=adapter,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )
    assert composition.provider_coordinator is None
    await composition.shutdown()


# --------------------------------------------------------------------------- #
# Live classifier is coverage-aware from the resolved dataset (ADR-011 LC5/LC6/LC17)
# --------------------------------------------------------------------------- #
def _live_utc(year: int, month: int, day: int) -> datetime:
    """06:30 UTC == 12:00 IST — mid-live-session on the same exchange-local date."""
    return datetime(year, month, day, 6, 30, tzinfo=UTC)


async def test_live_classifier_uses_dataset_calendar_and_coverage() -> None:
    # The runtime's classifier is built from the packaged 2026 dataset: out-of-coverage
    # dates are CALENDAR_UNAVAILABLE; the exceptional-OPEN Sunday 2026-02-01 is trading;
    # an ordinary Sunday is HOLIDAY.
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    classifier = composition.runtime.session_classifier
    assert classifier.classify(_live_utc(2027, 1, 1)).market_state is (
        MarketState.CALENDAR_UNAVAILABLE
    )
    assert classifier.classify(_live_utc(2026, 2, 1)).market_state is MarketState.LIVE_SESSION
    assert classifier.classify(_live_utc(2026, 1, 4)).market_state is MarketState.HOLIDAY
    await composition.shutdown()


async def test_live_candle_engine_keeps_default_settings_schedule() -> None:
    # LC17: the live CandleEngine keeps the settings SessionSchedule; the historical
    # EffectiveSchedule/overrides are never wired into it.
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    schedule = composition.runtime.candle_engine._schedule  # noqa: SLF001
    assert schedule.regular_open == time(9, 15)
    assert schedule.regular_close == time(15, 30)
    await composition.shutdown()


async def test_disabled_runtime_classifier_has_no_coverage_check() -> None:
    # The disabled/no-live runtime keeps the legacy settings classifier (coverage=None):
    # it carries no live data, so it is out of live-trading authority scope (contract).
    composition = await compose_market_runtime(
        settings=_disabled_settings(),
        error_threshold=_ERROR_THRESHOLD,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )
    classifier = composition.runtime.session_classifier
    # An out-of-2026 weekday is classified normally (no CALENDAR_UNAVAILABLE) — legacy.
    assert classifier.classify(_live_utc(2027, 1, 1)).market_state is MarketState.LIVE_SESSION


async def test_activation_adds_no_extra_task_or_duplicate_core() -> None:
    # §14 L: activating historical warmup composes exactly the two existing managed tasks
    # (ingestion + refresh driver) over one EventBus and one InstrumentStateRegistry.
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    runtime = composition.runtime
    bus, registry = runtime.bus, runtime.registry
    await composition.start()
    status = runtime.status()
    assert status.ingestion_running is True
    assert status.refresh_driver_running is True
    assert runtime.bus is bus  # single shared bus (no duplicate)
    assert runtime.registry is registry  # single shared registry (no duplicate)
    await composition.shutdown()


async def test_disabled_mode_builds_no_provider() -> None:
    composition = await compose_market_runtime(
        settings=_disabled_settings(),
        error_threshold=_ERROR_THRESHOLD,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )
    assert composition.provider_coordinator is None
    assert composition.runtime.status().known_instrument_count == 0


# --------------------------------------------------------------------------- #
# Enabled mode: lifecycle order, universe resolution, canonical boundary
# --------------------------------------------------------------------------- #
async def test_enabled_mode_resolves_and_composes_once() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE", "TCS", "INFY")))
    composition = await _compose(adapter)
    assert composition.provider_coordinator is not None
    assert adapter.calls == ["connect", "get_health", "load_instruments", "load_universe"]
    status = composition.runtime.status()
    assert status.known_instrument_count == 3


async def test_resolved_universe_is_known_to_the_runtime_registry() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE", "TCS")))
    composition = await _compose(adapter)
    registry = composition.runtime.registry
    assert registry.is_known(_instrument("RELIANCE"))
    assert registry.is_known(_instrument("TCS"))
    assert registry.is_known(_instrument("HDFCBANK")) is False


async def test_universe_ordering_is_preserved() -> None:
    symbols = ("AAA", "BBB", "CCC")
    adapter = _FakeAdapter(universe=_universe(symbols))
    composition = await _compose(adapter)
    # Every provided instrument is known; the provider's deterministic order is preserved.
    for symbol in symbols:
        assert composition.runtime.registry.is_known(_instrument(symbol))
    assert composition.runtime.status().known_instrument_count == len(symbols)


async def test_empty_universe_fails_closed_with_cleanup() -> None:
    adapter = _FakeAdapter(universe=_universe(()))
    with pytest.raises(UniverseResolutionError, match="empty"):
        await _compose(adapter)
    assert "disconnect" in adapter.calls  # provider cleaned up


async def test_duplicate_universe_fails_closed_with_cleanup() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE", "RELIANCE")))
    with pytest.raises(UniverseResolutionError, match="duplicate"):
        await _compose(adapter)
    assert "disconnect" in adapter.calls


async def test_production_authority_stays_disabled() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    status = composition.runtime.status()
    assert status.staged_observation_verified is False
    assert status.tick_aggregate_verified is False


# --------------------------------------------------------------------------- #
# Failure isolation & cleanup
# --------------------------------------------------------------------------- #
async def test_provider_start_failure_cleans_up_and_returns_no_runtime() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)), fail_on="connect")
    with pytest.raises(ProviderInitializationError):
        await _compose(adapter)
    assert "disconnect" in adapter.calls
    assert "load_instruments" not in adapter.calls  # never reached the universe


async def test_unhealthy_provider_fails_start() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)), health=ProviderStatus.DOWN)
    with pytest.raises(ProviderInitializationError):
        await _compose(adapter)
    assert "load_instruments" not in adapter.calls


async def test_instrument_master_failure_cleans_up() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)), fail_on="load_instruments")
    with pytest.raises(RuntimeError, match="load_instruments"):
        await _compose(adapter)
    assert "disconnect" in adapter.calls
    assert "load_universe" not in adapter.calls


async def test_universe_load_failure_cleans_up() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)), fail_on="load_universe")
    with pytest.raises(RuntimeError, match="load_universe"):
        await _compose(adapter)
    assert "disconnect" in adapter.calls


# --------------------------------------------------------------------------- #
# Lifecycle: start / shutdown ownership
# --------------------------------------------------------------------------- #
async def test_start_subscribes_the_manager_only_on_final_runtime() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    assert composition.runtime.manager_subscribed is False  # not started during build
    await composition.start()
    assert composition.runtime.manager_subscribed is True
    await composition.shutdown()  # cancel the ingestion task started on start()


async def test_shutdown_stops_runtime_then_provider() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    await composition.start()
    await composition.shutdown()
    assert composition.runtime.manager_subscribed is False
    assert "disconnect" in adapter.calls


async def test_shutdown_is_idempotent() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)
    await composition.start()
    await composition.shutdown()
    await composition.shutdown()  # safe
    assert composition.runtime.status().state.value == "shutdown"


# --------------------------------------------------------------------------- #
# Production factory path (construction only; no network)
# --------------------------------------------------------------------------- #
def test_default_factory_constructs_the_dhan_adapter() -> None:
    adapter = DhanRestAdapter.from_settings(_enabled_settings())
    assert isinstance(adapter, DhanRestAdapter)


def test_from_settings_threads_the_continuity_sink() -> None:
    sink = _DeferredContinuitySink()
    adapter = DhanRestAdapter.from_settings(_enabled_settings(), live_continuity_sink=sink)
    assert adapter._live_continuity_sink is sink  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Deferred continuity sink (construction-cycle resolution)
# --------------------------------------------------------------------------- #
def test_deferred_continuity_sink_forwards_after_bind() -> None:
    received: list[FeedContinuityEvent] = []
    sink = _DeferredContinuitySink()
    sink.bind(received.append)
    event = FeedContinuityEvent(status=FeedContinuity.RECONNECTED, observed_at=_NOW)
    sink(event)
    assert received == [event]


def test_deferred_continuity_sink_fails_closed_before_bind() -> None:
    sink = _DeferredContinuitySink()
    with pytest.raises(RuntimeError, match="before runtime binding"):
        sink(FeedContinuityEvent(status=FeedContinuity.RECONNECTED, observed_at=_NOW))


# --------------------------------------------------------------------------- #
# Secondary calendar monitor wiring (ADR-011; observation-only)
# --------------------------------------------------------------------------- #
async def test_enabled_calendar_monitor_is_composed_over_the_same_dataset() -> None:
    from app.services.calendar_monitor import CalendarMonitorService

    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    dataset = load_nse_cm_2026_dataset()
    composition = await compose_market_runtime(
        settings=_enabled_settings(calendar_monitor_enabled=True),
        error_threshold=_ERROR_THRESHOLD,
        adapter=adapter,
        calendar_dataset=dataset,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )
    monitor = composition.runtime._calendar_monitor  # noqa: SLF001
    assert isinstance(monitor, CalendarMonitorService)
    # The monitor and the coverage-aware live classifier share the SAME resolved dataset.
    assert monitor._dataset is dataset  # noqa: SLF001
    classifier = composition.runtime.session_classifier
    assert classifier.classify(_live_utc(2027, 1, 1)).market_state is (
        MarketState.CALENDAR_UNAVAILABLE
    )
    await composition.shutdown()


async def test_disabled_calendar_monitor_composes_no_monitor() -> None:
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    composition = await _compose(adapter)  # calendar_monitor_enabled defaults False
    assert composition.runtime._calendar_monitor is None  # noqa: SLF001
    assert composition.runtime.status().calendar_monitor_configured is False
    await composition.shutdown()


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
async def test_composition_is_deterministic_for_the_same_universe() -> None:
    first = await _compose(_FakeAdapter(universe=_universe(("RELIANCE", "TCS"))))
    second = await _compose(_FakeAdapter(universe=_universe(("RELIANCE", "TCS"))))
    assert first.runtime.status() == second.runtime.status()


def test_composed_narrow_cpr_scanner_policy_matches_strategy_output() -> None:
    # §5/§41 drift-catch: the composed narrow_cpr ranking policy's metric name must equal a
    # metric the REAL NarrowCprStrategy emits, so the generic scanner ranks it with no code change.
    from datetime import date, timedelta
    from decimal import Decimal

    from app.market_engine.context import MarketContext
    from app.market_engine.historical.context import HistoricalContext, PreviousSessionFacts
    from app.schemas.market_data import Candle
    from app.services.strategy_catalog import production_catalog
    from app.strategies.contracts import StrategyEvaluationMetadata
    from app.strategies.enums import StrategyTrigger
    from app.strategies.implementations.narrow_cpr import NarrowCprConfiguration, NarrowCprStrategy

    strategy = NarrowCprStrategy()
    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    candle = Candle(
        instrument=instrument,
        start_timestamp=_NOW,
        end_timestamp=_NOW + timedelta(hours=6),
        open_price=Decimal("100"),
        high_price=Decimal("120"),
        low_price=Decimal("68"),
        close_price=Decimal("112"),
        traded_quantity=100,
    )
    context = MarketContext(
        instrument=instrument,
        version=1,
        sequence=1,
        event_timestamp=_NOW,
        observed_at=_NOW,
        historical=HistoricalContext(
            instrument=instrument,
            previous_session=PreviousSessionFacts(trading_date=date(2026, 2, 6), candle=candle),
        ),
    )
    metadata = StrategyEvaluationMetadata(
        trigger=StrategyTrigger.ON_HISTORICAL_READY,
        context_version=1,
        observed_at=_NOW,
        trading_date=date(2026, 8, 6),
    )
    evaluation = strategy.evaluate(
        context, NarrowCprConfiguration(config_version="1.0.0"), metadata
    )
    emitted = {metric.name for metric in evaluation.metrics}
    entry = production_catalog().resolve(("narrow_cpr",))[0]
    assert entry.ranking_policy is not None
    assert entry.ranking_policy.strategy_id == strategy.descriptor.strategy_id
    assert entry.ranking_policy.metric_name in emitted


# --------------------------------------------------------------------------- #
# Production strategy enablement via the catalog (ADR-013)
# --------------------------------------------------------------------------- #
async def test_no_strategies_enabled_leaves_scanner_inert() -> None:
    # Default strategies_enabled="" → zero strategies registered/started → scanner inert.
    composition = await _compose(_FakeAdapter(universe=_universe(("RELIANCE",))))
    await composition.start()
    assert composition.runtime.scanner_snapshot("narrow_cpr") is None
    await composition.shutdown()


async def test_enabled_narrow_cpr_registers_and_enters_requirement_union() -> None:
    # strategies_enabled="narrow_cpr" → the strategy is registered and started, so its
    # session historical requirement enters the effective union (REG9). Warmup over the fake
    # provider may leave it ERROR, but registration puts the requirement in the union first.
    settings = _enabled_settings(strategies_enabled="narrow_cpr")
    composition = await _compose(_FakeAdapter(universe=_universe(("RELIANCE",))), settings)
    await composition.start()
    reqs = composition.runtime.historical_requirements.effective_requirements()
    assert any(req.timeframe.is_session and req.lookback == 1 for req in reqs)
    await composition.shutdown()


async def test_unknown_enabled_strategy_fails_closed_and_cleans_up() -> None:
    from app.services.strategy_catalog import UnknownEnabledStrategyError

    settings = _enabled_settings(strategies_enabled="ghost")
    adapter = _FakeAdapter(universe=_universe(("RELIANCE",)))
    with pytest.raises(UnknownEnabledStrategyError):
        await _compose(adapter, settings)
    assert adapter.calls.count("disconnect") == 1  # provider coordinator cleaned up
