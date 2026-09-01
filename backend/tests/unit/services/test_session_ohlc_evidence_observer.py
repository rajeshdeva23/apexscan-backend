"""ADR-015 tests for the single-account in-process Session-OHLC evidence observer (R4D B3).

Deterministic and offline: a real in-process ``EventBus`` plus fakes for the classifier,
clock, and the broker-neutral ``SessionStatisticsSource``. No Dhan, no network, no real socket.
Covers the OBS-01..OBS-30 matrix: disabled wiring, hot-path safety/isolation, universe freeze,
window capture/gating, REST reuse/failure, late-attach, natural-reconnect provenance, atomic
artifacts, provenance, schema 2.2.0, and protocol-equivalence.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.events.bus import EventBus
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextUpdated
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    ProviderSessionOhlc,
    SessionStatisticsObservation,
    Tick,
)
from app.services import session_ohlc_evidence_observer as observer_module
from app.services.session_ohlc_evidence_observer import (
    SessionOhlcEvidenceObserver,
    _due_window,
)
from app.tools.session_ohlc_evidence.models import LateStartKind

_D = date(2026, 8, 31)
_T = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
_TZ = "Asia/Kolkata"


def _inst(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _context(symbol: str, o: str, h: str, low: str, *, version: int = 1) -> MarketContext:
    tick = Tick(
        instrument=_inst(symbol),
        event_timestamp=_T,
        last_price=Decimal(o),
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal(o),
            high_price=Decimal(h),
            low_price=Decimal(low),
            close_price=Decimal(o),
        ),
    )
    return MarketContext(
        instrument=_inst(symbol),
        version=version,
        sequence=version,
        event_timestamp=_T,
        observed_at=_T,
        latest_tick=tick,
        session=SessionContext(
            trading_date=_D, market_state=MarketState.LIVE_SESSION, exchange_timezone=_TZ
        ),
    )


def _obs(symbol: str, o: str, h: str, low: str) -> SessionStatisticsObservation:
    return SessionStatisticsObservation(
        instrument=_inst(symbol),
        trading_date=_D,
        observed_at=_T,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal(o),
            high_price=Decimal(h),
            low_price=Decimal(low),
            close_price=Decimal(o),
        ),
    )


class _FakeSource:
    """A broker-neutral SessionStatisticsSource stand-in that records calls."""

    def __init__(
        self,
        observations: tuple[SessionStatisticsObservation, ...] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self._observations = observations
        self._error = error
        self.calls = 0

    async def load_session_statistics(self, instruments, *, trading_date, observed_at):  # noqa: ANN001
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._observations


class _Classifier:
    """A fake classifier returning a fixed market state + trading date."""

    def __init__(
        self, state: MarketState = MarketState.LIVE_SESSION, trading_date: date = _D
    ) -> None:
        self.state = state
        self.trading_date = trading_date

    def classify(self, instant: datetime) -> SessionContext:
        return SessionContext(
            trading_date=self.trading_date, market_state=self.state, exchange_timezone=_TZ
        )


def _observer(
    bus: EventBus,
    *,
    classifier: _Classifier,
    source: _FakeSource,
    universe: tuple[Instrument, ...],
    artifact_root: Path,
    production_image: str | None = "apexscan-backend:testsha",
) -> SessionOhlcEvidenceObserver:
    return SessionOhlcEvidenceObserver(
        bus=bus,
        classifier=classifier.classify,
        clock=lambda: _T,
        statistics_source=source,  # type: ignore[arg-type]
        universe=universe,
        artifact_root=artifact_root,
        source_sha=production_image or "unknown",
        production_image=production_image,
        exchange_timezone=_TZ,
    )


# --------------------------------------------------------------------------- #
# OBS-01 / OBS-22 — enablement gate (composition factory)
# --------------------------------------------------------------------------- #
def test_obs_01_factory_none_when_flag_disabled() -> None:
    from app.core.config import get_settings
    from app.services.dhan_runtime_composition import _evidence_observer_factory

    settings = get_settings()
    assert settings.session_ohlc_evidence_observer_enabled is False
    factory = _evidence_observer_factory(
        settings=settings,
        provider=None,  # type: ignore[arg-type]
        session_classifier=None,  # type: ignore[arg-type]
        universe=(),
        clock=None,
    )
    assert (
        factory is None
    )  # disabled => no observer, no subscription, no task, no REST, no artifact


def test_obs_22_runtime_without_factory_has_no_observer() -> None:
    from app.core.config import get_settings
    from app.services.market_runtime import LiveMarketRuntime

    runtime = LiveMarketRuntime(settings=get_settings(), error_threshold=3)
    assert runtime._evidence_observer is None  # noqa: SLF001 - regression guard


async def test_obs_01b_runtime_task_topology_off_vs_on(tmp_path: Path) -> None:
    # Proves the topology gate at RUNTIME (not just source-count): flag OFF => no observer task;
    # factory present (flag ON) => exactly the observer task is added and cancelled on shutdown.
    from app.core.config import get_settings
    from app.services.market_runtime import LiveMarketRuntime

    settings = get_settings()

    off = LiveMarketRuntime(settings=settings, error_threshold=3)
    await off.start()
    assert off._evidence_observer_task is None  # noqa: SLF001 - OFF: no evidence task
    await off.shutdown()

    def factory(bus: EventBus) -> SessionOhlcEvidenceObserver:
        return _observer(
            bus,
            classifier=_Classifier(state=MarketState.MARKET_CLOSED),
            source=_FakeSource(),
            universe=(),
            artifact_root=tmp_path,
        )

    on = LiveMarketRuntime(settings=settings, error_threshold=3, evidence_observer_factory=factory)
    await on.start()
    task = on._evidence_observer_task  # noqa: SLF001
    assert task is not None and not task.done()  # ON: exactly the observer task exists
    await on.shutdown()
    assert task.cancelled() or task.done()  # cleanly cancelled on shutdown (no leak)


# --------------------------------------------------------------------------- #
# OBS-02..06 — hot-path callback contract + failure isolation
# --------------------------------------------------------------------------- #
def test_obs_02_03_26_27_callback_does_no_rest_disk_or_eval(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    assert source.calls == 0  # no REST in callback
    assert list(tmp_path.iterdir()) == []  # no disk write in callback


def test_obs_04_bounded_one_snapshot_per_instrument(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource(),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    for v in range(1, 6):
        bus.publish(
            MarketContextUpdated(
                context=_context("AAA", "100", str(100 + v), "99", version=v),
                previous_version=v - 1,
            )
        )
    assert len(obs._latest) == 1  # noqa: SLF001 - O(universe), not O(ticks)
    assert obs._latest["NSE:AAA"].high_price == Decimal("105")  # noqa: SLF001 - latest replaces


def test_obs_05_snapshot_is_immutable_value() -> None:
    snap = observer_module._Snapshot(
        identity="NSE:AAA",
        exchange="NSE",
        symbol="AAA",
        trading_date=_D,
        event_timestamp=_T,
        observed_at=_T,
        version=1,
        open_price=Decimal("1"),
        high_price=Decimal("2"),
        low_price=Decimal("1"),
    )
    with pytest.raises((AttributeError, TypeError)):
        snap.high_price = Decimal("9")  # type: ignore[misc] - frozen dataclass


def test_obs_06_callback_exception_never_breaks_publish(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource(),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()

    def _boom(_context: MarketContext) -> None:
        raise RuntimeError("evidence bug")

    monkeypatch.setattr(obs, "_record_snapshot", _boom)
    downstream: list[int] = []
    bus.subscribe(MarketContextUpdated, lambda e: downstream.append(e.previous_version))
    # publish must not raise even though the evidence handler throws internally
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=7)
    )
    assert downstream == [7]  # a co-subscriber (stand-in for ingestion) still ran


# --------------------------------------------------------------------------- #
# OBS-07..12 — window capture, gating, universe freeze, coverage
# --------------------------------------------------------------------------- #
async def test_obs_08_early_capture_and_coverage(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource((_obs("AAA", "100", "101", "99"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"), _inst("BBB")),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    result = await obs.capture_window("early")
    assert result.executed and result.observed == 1 and result.pending == 1  # BBB pending
    assert source.calls == 1  # exactly one REST call per window


async def test_obs_07_universe_frozen_per_session(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource(),
        universe=(_inst("AAA"), _inst("BBB")),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    await obs.capture_window("early")
    frozen = obs._frozen_universe  # noqa: SLF001
    await obs.capture_window("mid")
    assert obs._frozen_universe == frozen == ("NSE:AAA", "NSE:BBB")  # noqa: SLF001


async def test_obs_11_no_capture_outside_live_session(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(state=MarketState.MARKET_CLOSED),
        source=_FakeSource(),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    result = await obs.capture_window("early")
    assert not result.executed and "LIVE_SESSION" in result.reason


def test_obs_23_due_window_cadence() -> None:
    from datetime import time

    assert _due_window(time(9, 25)) == "early"
    assert _due_window(time(12, 15)) == "mid"
    assert _due_window(time(15, 10)) == "late"
    assert _due_window(time(11, 0)) is None  # between windows
    assert _due_window(time(16, 0)) is None  # after close


async def test_obs_28_duplicate_window_not_recaptured(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource((_obs("AAA", "100", "101", "99"),)),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    first = await obs.capture_window("early")
    second = await obs.capture_window("early")
    assert first.executed and not second.executed and "already" in second.reason


# --------------------------------------------------------------------------- #
# OBS-13..15 — REST reuse / failure isolation
# --------------------------------------------------------------------------- #
async def test_obs_13_uses_injected_source_instance(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource((_obs("AAA", "100", "101", "99"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    await obs.capture_window("early")
    assert source.calls == 1  # the exact injected source was used


async def test_obs_15_rest_failure_is_evidence_incompleteness(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource(error=TimeoutError("provider slow"))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    result = await obs.capture_window("early")  # must not raise
    assert result.executed  # window still recorded; comparisons simply absent
    record = obs._partials[-1]  # noqa: SLF001
    assert record.instruments[0].oracle_comparisons == ()  # no REST -> no comparisons


def test_obs_14_19_no_second_auth_or_ws_in_source() -> None:
    src = inspect.getsource(observer_module)
    for forbidden in (
        "from_settings",
        "DhanAuthManager",
        "generateAccessToken",
        "websocket",
        "stream_market_data",
        "DhanRestAdapter",
    ):
        assert forbidden not in src


# --------------------------------------------------------------------------- #
# OBS-16 — observer-late-attach
# --------------------------------------------------------------------------- #
async def test_obs_16_late_attach_recorded_and_labeled(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource((_obs("AAA", "100", "104", "97"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "105", "96"), previous_version=0)
    )
    await obs.capture_window("early")
    late = obs._partials[-1].late_start  # noqa: SLF001
    assert late is not None and late.observed
    assert late.kind is LateStartKind.OBSERVER_LATE_ATTACH  # never provider_late_subscription
    assert late.observer_attach_timestamp is not None


# --------------------------------------------------------------------------- #
# OBS-17 / OBS-18 — natural reconnect provenance / no reconnect
# --------------------------------------------------------------------------- #
async def test_obs_17_natural_reconnect_provenance(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource(),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "104", "97"), previous_version=0)
    )
    obs.on_feed_continuity(FeedContinuityEvent(status=FeedContinuity.DISCONNECTED, observed_at=_T))
    bus.publish(
        MarketContextUpdated(
            context=_context("AAA", "100", "106", "95", version=2), previous_version=1
        )
    )
    obs.on_feed_continuity(FeedContinuityEvent(status=FeedContinuity.RECONNECTED, observed_at=_T))
    assert obs._reconnect is not None and obs._reconnect.observed  # noqa: SLF001
    assert obs._reconnect.pre.high_price == Decimal("104")  # noqa: SLF001
    assert obs._reconnect.post.high_price == Decimal("106")  # noqa: SLF001


async def test_obs_18_no_reconnect_is_inconclusive(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource((_obs("AAA", "100", "101", "99"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    for window in ("early", "mid", "late"):
        await obs.capture_window(window)
    combined = obs.finalize_session()
    assert combined is not None
    assert combined.reconnect is not None and not combined.reconnect.observed
    assert "NO_NATURAL_RECONNECT" in combined.reconnect.detail


# --------------------------------------------------------------------------- #
# OBS-20 / OBS-21 / OBS-24 / OBS-25 — schema, protocol-equiv, atomic write, provenance
# --------------------------------------------------------------------------- #
async def test_obs_20_25_schema_version_and_provenance(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource((_obs("AAA", "100", "101", "99"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
        production_image="apexscan-backend:deadbee",
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    await obs.capture_window("early")
    record = obs._partials[-1]  # noqa: SLF001
    assert record.schema_version == "2.2.0"
    assert record.production_image == "apexscan-backend:deadbee"  # runtime-derived, not fabricated


async def test_obs_21_protocol_equivalence_holds(tmp_path: Path) -> None:
    bus = EventBus()
    # WS float32 expansion vs clean REST 2dp for the same wire price.
    source = _FakeSource((_obs("AAA", "212.37", "215.0", "210.0"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(
            context=_context("AAA", "212.3699951171875", "215.0", "210.0"), previous_version=0
        )
    )
    await obs.capture_window("early")
    comparisons = obs._partials[-1].instruments[0].oracle_comparisons  # noqa: SLF001
    open_cmp = next(c for c in comparisons if c.field == "open")
    assert open_cmp.classification.value == "protocol_equivalent"


async def test_obs_24_finalize_writes_atomic_artifacts(tmp_path: Path) -> None:
    bus = EventBus()
    source = _FakeSource((_obs("AAA", "100", "101", "99"),))
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=source,
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    for window in ("early", "mid", "late"):
        await obs.capture_window(window)
    obs.finalize_session()
    session_dir = tmp_path / _D.isoformat()
    assert (session_dir / "combined.json").exists()
    assert (session_dir / "combined.md").exists()
    assert not list(session_dir.glob("*.tmp"))  # no leftover temp files


# --------------------------------------------------------------------------- #
# OBS-29 / OBS-30 — trading-date rollover / clean shutdown
# --------------------------------------------------------------------------- #
async def test_obs_29_trading_date_rollover_resets_session(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource(),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    await obs.capture_window("early")
    assert obs._window_phase == {"early": "finalized"}  # noqa: SLF001
    obs._classify = _Classifier(trading_date=date(2026, 9, 1)).classify  # noqa: SLF001 - roll date
    await obs.capture_window("early")
    assert obs._session_trading_date == date(2026, 9, 1)  # noqa: SLF001
    assert obs._window_phase == {
        "early": "finalized"
    }  # reset then re-captured for the new date  # noqa: SLF001


def test_obs_30_unsubscribe_is_clean_and_idempotent(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _observer(
        bus,
        classifier=_Classifier(),
        source=_FakeSource(),
        universe=(_inst("AAA"),),
        artifact_root=tmp_path,
    )
    obs.subscribe()
    obs.unsubscribe()
    obs.unsubscribe()  # idempotent
    # after unsubscribe, a publish reaches no observer handler (no snapshot recorded)
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    assert obs._latest == {}  # noqa: SLF001


# --------------------------------------------------------------------------- #
# B7 — window-capture timing state machine (NOT_STARTED -> COLLECTING -> FINALIZED)
# --------------------------------------------------------------------------- #
def _json_files(root: Path, pattern: str = "*.json") -> list[Path]:
    """Sync artifact lister (keeps Path I/O out of async test bodies; ASYNC240)."""
    return list(root.rglob(pattern))


def _mid_clock() -> datetime:
    # 12:13 IST == 06:43 UTC — inside the MID band (12:00-12:30 IST).
    return datetime(2026, 9, 1, 6, 43, 0, tzinfo=UTC)


def _b7_observer(bus: EventBus, tmp_path: Path, *, universe, deadline=300.0):  # noqa: ANN001,ANN202
    return SessionOhlcEvidenceObserver(
        bus=bus,
        classifier=_Classifier().classify,
        clock=_mid_clock,
        statistics_source=_FakeSource(tuple(_obs(s, "100", "101", "99") for s in universe)),
        universe=tuple(_inst(s) for s in universe),
        artifact_root=tmp_path,
        source_sha="4d4e653",
        production_image="4d4e653",
        exchange_timezone=_TZ,
        per_window_seconds=300.0,
    )


async def test_b7_01_starts_collecting_not_finalized_when_started_in_window(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _b7_observer(bus, tmp_path, universe=["AAA", "BBB"])
    obs.subscribe()
    await obs._tick_once()  # noqa: SLF001 - first poll inside MID with empty snapshot
    assert obs._window_phase.get("mid") == "collecting"  # noqa: SLF001 - NOT finalized
    assert _json_files(tmp_path) == []  # B7-02: no artifact yet


async def test_b7_02_first_poll_does_not_consume_window(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _b7_observer(bus, tmp_path, universe=["AAA", "BBB"])
    obs.subscribe()
    await obs._tick_once()  # noqa: SLF001
    await obs._tick_once()  # noqa: SLF001 - still collecting; only AAA seen so far -> partial
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    await obs._tick_once()  # noqa: SLF001 - coverage still incomplete (BBB missing)
    assert obs._window_phase.get("mid") == "collecting"  # noqa: SLF001


async def test_b7_04_full_coverage_finalizes_early_once(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _b7_observer(bus, tmp_path, universe=["AAA", "BBB"])
    obs.subscribe()
    await obs._tick_once()  # noqa: SLF001 - begin collecting
    for s in ("AAA", "BBB"):
        bus.publish(
            MarketContextUpdated(context=_context(s, "100", "101", "99"), previous_version=0)
        )
    await obs._tick_once()  # noqa: SLF001 - full coverage -> finalize
    assert obs._window_phase.get("mid") == "finalized"  # noqa: SLF001
    rec = obs._partials[-1]  # noqa: SLF001
    assert len(rec.instruments) == 2 and len(rec.pending_instruments) == 0
    await obs._tick_once()  # noqa: SLF001 - B7-06/07: no second finalize
    assert len([p for p in obs._partials if "mid" in p.sample_windows]) == 1  # noqa: SLF001


async def test_b7_05_deadline_finalizes_with_pending(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _b7_observer(bus, tmp_path, universe=["AAA", "BBB"])
    obs.subscribe()
    await obs._tick_once()  # noqa: SLF001 - begin collecting (deadline = now + 300s)
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )
    # jump the clock past the deadline; BBB never arrived
    obs._now = lambda: datetime(2026, 9, 1, 6, 49, 0, tzinfo=UTC)  # noqa: SLF001 - +6min
    await obs._tick_once()  # noqa: SLF001
    assert obs._window_phase.get("mid") == "finalized"  # noqa: SLF001
    rec = obs._partials[-1]  # noqa: SLF001
    assert len(rec.instruments) == 1 and rec.pending_instruments == ("NSE:BBB",)


async def test_b7_13_market_closed_does_not_start_capture(tmp_path: Path) -> None:
    bus = EventBus()
    obs = _b7_observer(bus, tmp_path, universe=["AAA"])
    obs._classify = _Classifier(state=MarketState.MARKET_CLOSED).classify  # noqa: SLF001
    obs.subscribe()
    await obs._tick_once()  # noqa: SLF001
    assert obs._window_phase == {}  # noqa: SLF001 - no window opened
    assert _json_files(tmp_path) == []


async def test_b7_15_write_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    bus = EventBus()
    obs = _b7_observer(bus, tmp_path, universe=["AAA"])
    obs.subscribe()
    await obs._tick_once()  # noqa: SLF001 - collecting
    bus.publish(
        MarketContextUpdated(context=_context("AAA", "100", "101", "99"), previous_version=0)
    )

    def _boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(obs, "_write_record_json", _boom)
    with pytest.raises(OSError):
        await obs._finalize_window("mid", _D)  # noqa: SLF001 - write fails
    assert obs._window_phase.get("mid") == "collecting"  # noqa: SLF001 - NOT marked finalized
    assert _json_files(tmp_path, "mid.json") == []  # no valid-looking artifact
