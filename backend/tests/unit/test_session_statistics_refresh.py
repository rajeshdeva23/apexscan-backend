"""SessionStatisticsRefreshService: batch refresh, validation, staging (P4.6E4; ADR-009).

Uses a fake source and a real InstrumentStateRegistry — no network, no real credentials,
no wall clock (an injected ManualClock supplies observed_at).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.adapters.base import ProviderContractViolationError, ProviderUnavailableError
from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import SessionStatisticsQuality
from app.market_engine.events import MarketContextCreated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.session_statistics import SessionStatisticsAuthority
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.schemas.market_data import (
    Instrument,
    ProviderSessionOhlc,
    SessionStatisticsObservation,
    Tick,
)
from app.services.session_statistics_refresh import SessionStatisticsRefreshService

_DATE = date(2026, 8, 6)
_NEXT_DATE = date(2026, 8, 7)
_AT = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_AT2 = _AT + timedelta(minutes=1)


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
    instrument: Instrument,
    *,
    trading_date: date = _DATE,
    observed_at: datetime = _AT,
    high: str = "105",
) -> SessionStatisticsObservation:
    return SessionStatisticsObservation(
        instrument=instrument,
        trading_date=trading_date,
        observed_at=observed_at,
        session_ohlc=_ohlc(high=high),
    )


_Responder = Callable[
    [tuple[Instrument, ...], date, datetime], Sequence[SessionStatisticsObservation]
]


@dataclass
class _FakeSource:
    responder: _Responder
    calls: list[tuple[tuple[Instrument, ...], date, datetime]] = field(default_factory=list)

    async def load_session_statistics(
        self,
        instruments: Sequence[Instrument],
        *,
        trading_date: date,
        observed_at: datetime,
    ) -> tuple[SessionStatisticsObservation, ...]:
        self.calls.append((tuple(instruments), trading_date, observed_at))
        return tuple(self.responder(tuple(instruments), trading_date, observed_at))


def _echo(
    instruments: tuple[Instrument, ...], trading_date: date, observed_at: datetime
) -> tuple[SessionStatisticsObservation, ...]:
    return tuple(
        _obs(instrument, trading_date=trading_date, observed_at=observed_at)
        for instrument in instruments
    )


def _service(
    source: _FakeSource,
    *,
    symbols: Sequence[str] = ("RELIANCE", "TCS"),
    clock: ManualClock | None = None,
) -> tuple[SessionStatisticsRefreshService, InstrumentStateRegistry]:
    registry = InstrumentStateRegistry(_instrument(s) for s in symbols)
    service = SessionStatisticsRefreshService(
        source=source, registry=registry, clock=clock or ManualClock(_AT)
    )
    return service, registry


def _staged(registry: InstrumentStateRegistry, symbol: str) -> SessionStatisticsObservation | None:
    state = registry.get(_instrument(symbol))
    return state.staged_session_statistics_observation if state is not None else None


# --------------------------------------------------------------------------- #
# Batch / one-call semantics
# --------------------------------------------------------------------------- #
async def test_empty_request_makes_no_source_call() -> None:
    source = _FakeSource(_echo)
    service, _ = _service(source)
    outcome = await service.refresh([], trading_date=_DATE)
    assert source.calls == []
    assert (outcome.requested_count, outcome.observation_count, outcome.staged_count) == (0, 0, 0)


async def test_single_instrument_one_source_call_and_staged() -> None:
    source = _FakeSource(_echo)
    service, registry = _service(source)
    outcome = await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert len(source.calls) == 1
    assert outcome.staged_count == 1
    assert _staged(registry, "RELIANCE") is not None


async def test_many_instruments_one_logical_source_call() -> None:
    source = _FakeSource(_echo)
    symbols = tuple(f"SYM{i:03d}" for i in range(208))
    service, _ = _service(source, symbols=symbols)
    outcome = await service.refresh([_instrument(s) for s in symbols], trading_date=_DATE)
    assert len(source.calls) == 1
    assert len(source.calls[0][0]) == 208
    assert outcome.staged_count == 208


async def test_duplicate_requested_instruments_are_deduplicated() -> None:
    source = _FakeSource(_echo)
    service, _ = _service(source)
    inst = _instrument("RELIANCE")
    outcome = await service.refresh([inst, inst], trading_date=_DATE)
    assert outcome.requested_count == 1
    assert len(source.calls[0][0]) == 1


async def test_input_is_canonically_ordered() -> None:
    source = _FakeSource(_echo)
    service, _ = _service(source)
    await service.refresh([_instrument("TCS"), _instrument("RELIANCE")], trading_date=_DATE)
    assert [i.symbol for i in source.calls[0][0]] == ["RELIANCE", "TCS"]


async def test_trading_date_is_passed_through_exactly() -> None:
    source = _FakeSource(_echo)
    service, _ = _service(source)
    await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert source.calls[0][1] == _DATE


# --------------------------------------------------------------------------- #
# Clock / observed_at
# --------------------------------------------------------------------------- #
@dataclass
class _CountingClock:
    _at: datetime
    count: int = 0

    def now(self) -> datetime:
        self.count += 1
        return self._at


async def test_clock_is_read_once_per_refresh_not_per_instrument() -> None:
    clock = _CountingClock(_AT)
    source = _FakeSource(_echo)
    registry = InstrumentStateRegistry(_instrument(s) for s in ("RELIANCE", "TCS"))
    service = SessionStatisticsRefreshService(source=source, registry=registry, clock=clock)
    await service.refresh([_instrument("RELIANCE"), _instrument("TCS")], trading_date=_DATE)
    assert clock.count == 1
    assert source.calls[0][2] == _AT  # the single governed observed_at


async def test_observed_at_is_utc_aware() -> None:
    ist = ManualClock(datetime(2026, 8, 6, 12, 0, tzinfo=UTC))
    source = _FakeSource(_echo)
    service, _ = _service(source, clock=ist)
    outcome = await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert outcome.observed_at.tzinfo is UTC


# --------------------------------------------------------------------------- #
# Source-result validation (fail closed before staging)
# --------------------------------------------------------------------------- #
async def test_unrequested_observation_is_rejected_and_stages_nothing() -> None:
    def responder(instruments, trading_date, observed_at):  # noqa: ANN001, ARG001
        return (_obs(_instrument("INFY"), trading_date=trading_date, observed_at=observed_at),)

    source = _FakeSource(responder)
    service, registry = _service(source)
    with pytest.raises(ProviderContractViolationError):
        await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert _staged(registry, "RELIANCE") is None


async def test_duplicate_observation_is_rejected() -> None:
    def responder(instruments, trading_date, observed_at):  # noqa: ANN001, ARG001
        inst = _instrument("RELIANCE")
        return (
            _obs(inst, trading_date=trading_date, observed_at=observed_at),
            _obs(inst, trading_date=trading_date, observed_at=observed_at, high="108"),
        )

    source = _FakeSource(responder)
    service, registry = _service(source)
    with pytest.raises(ProviderContractViolationError):
        await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert _staged(registry, "RELIANCE") is None


async def test_wrong_trading_date_observation_is_rejected() -> None:
    def responder(instruments, trading_date, observed_at):  # noqa: ANN001, ARG001
        return (_obs(_instrument("RELIANCE"), trading_date=_NEXT_DATE, observed_at=observed_at),)

    source = _FakeSource(responder)
    service, registry = _service(source)
    with pytest.raises(ProviderContractViolationError):
        await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert _staged(registry, "RELIANCE") is None


# --------------------------------------------------------------------------- #
# Partial / failure / cancellation
# --------------------------------------------------------------------------- #
async def test_partial_result_stages_only_returned_observations() -> None:
    def responder(instruments, trading_date, observed_at):  # noqa: ANN001, ARG001
        return (_obs(_instrument("RELIANCE"), trading_date=trading_date, observed_at=observed_at),)

    source = _FakeSource(responder)
    service, registry = _service(source)
    outcome = await service.refresh(
        [_instrument("RELIANCE"), _instrument("TCS")], trading_date=_DATE
    )
    assert (outcome.requested_count, outcome.observation_count) == (2, 1)
    assert _staged(registry, "RELIANCE") is not None
    assert _staged(registry, "TCS") is None


async def test_missing_result_does_not_clear_previously_staged_state() -> None:
    source = _FakeSource(_echo)
    service, registry = _service(source)
    registry.stage_session_statistics_observation(_instrument("TCS"), _obs(_instrument("TCS")))
    await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)  # TCS not requested
    assert _staged(registry, "TCS") is not None  # untouched


async def test_source_exception_propagates_and_stages_nothing() -> None:
    def responder(instruments, trading_date, observed_at):  # noqa: ANN001, ARG001
        raise ProviderUnavailableError

    source = _FakeSource(responder)
    service, registry = _service(source)
    with pytest.raises(ProviderUnavailableError):
        await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert _staged(registry, "RELIANCE") is None


async def test_cancellation_propagates_and_stages_nothing() -> None:
    def responder(instruments, trading_date, observed_at):  # noqa: ANN001, ARG001
        raise asyncio.CancelledError

    source = _FakeSource(responder)
    service, registry = _service(source)
    with pytest.raises(asyncio.CancelledError):
        await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert _staged(registry, "RELIANCE") is None


# --------------------------------------------------------------------------- #
# Registry policy through the service / no-context / isolation / replay
# --------------------------------------------------------------------------- #
async def test_refresh_creates_no_market_context_version() -> None:
    source = _FakeSource(_echo)
    service, registry = _service(source)
    await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert registry.get(_instrument("RELIANCE")).context is None  # type: ignore[union-attr]


async def test_older_refresh_cannot_replace_newer_staged_state() -> None:
    source = _FakeSource(_echo)
    clock = ManualClock(_AT2)
    service, registry = _service(source, clock=clock)
    await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)  # observed_at = _AT2
    clock.set(_AT)  # rewind to an older instant
    await service.refresh(
        [_instrument("RELIANCE")], trading_date=_DATE
    )  # observed_at = _AT (older)
    assert _staged(registry, "RELIANCE").observed_at == _AT2  # type: ignore[union-attr]


async def test_same_instant_conflicting_observation_follows_registry_fail_closed() -> None:
    responses = iter(
        [
            lambda i, d, o: (
                _obs(_instrument("RELIANCE"), trading_date=d, observed_at=o, high="105"),
            ),
            lambda i, d, o: (
                _obs(_instrument("RELIANCE"), trading_date=d, observed_at=o, high="108"),
            ),
        ]
    )

    def responder(instruments, trading_date, observed_at):  # noqa: ANN001
        return next(responses)(instruments, trading_date, observed_at)

    source = _FakeSource(responder)
    service, _ = _service(source, clock=ManualClock(_AT))
    await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    with pytest.raises(ValueError, match="conflicting"):
        await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)


async def test_multi_instrument_staging_isolation() -> None:
    source = _FakeSource(_echo)
    service, registry = _service(source)
    await service.refresh([_instrument("RELIANCE"), _instrument("TCS")], trading_date=_DATE)
    assert _staged(registry, "RELIANCE") is not None
    assert _staged(registry, "TCS") is not None


async def test_replay_is_deterministic() -> None:
    async def run() -> list[SessionStatisticsObservation | None]:
        source = _FakeSource(_echo)
        service, registry = _service(source, clock=ManualClock(_AT))
        await service.refresh([_instrument("RELIANCE"), _instrument("TCS")], trading_date=_DATE)
        return [_staged(registry, "RELIANCE"), _staged(registry, "TCS")]

    assert await run() == await run()


# --------------------------------------------------------------------------- #
# End-to-end: refresh stages → next accepted datum surfaces via the Market Engine (E2)
# --------------------------------------------------------------------------- #
async def test_staged_refresh_surfaces_on_next_accepted_datum() -> None:
    schedule = SessionSchedule(
        pre_open_start=time(9, 0),
        opening_auction_start=time(9, 8),
        regular_open=time(9, 15),
        regular_close=time(15, 30),
        closing_end=time(15, 40),
    )
    classifier = MarketSessionClassifier(
        schedule=schedule, calendar=TradingCalendar(), exchange_timezone="Asia/Kolkata"
    )
    registry = InstrumentStateRegistry([_instrument("RELIANCE")])
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(datetime(2026, 8, 6, 12, 0, tzinfo=UTC)),
        sequence=MonotonicSequence(),
        session=classifier,
        session_statistics_authority=SessionStatisticsAuthority(staged_observation_verified=True),
    )
    service = SessionStatisticsRefreshService(
        source=_FakeSource(_echo), registry=registry, clock=ManualClock(_AT)
    )

    await service.refresh([_instrument("RELIANCE")], trading_date=_DATE)
    assert len(recorded) == 0  # refresh alone mints no version/event

    tick = Tick(instrument=_instrument("RELIANCE"), event_timestamp=_AT, last_price=Decimal("100"))
    result = engine.process(tick)
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.quality is SessionStatisticsQuality.AUTHORITATIVE
    assert result.context.session_statistics.high_price == Decimal("105")
    assert [type(e) for e in recorded] == [MarketContextCreated]  # one version, one event
