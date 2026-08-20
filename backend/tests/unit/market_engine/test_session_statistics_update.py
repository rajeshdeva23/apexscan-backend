"""Pure session-statistics update logic: phases, authority, progression, reset (P4.6B)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.market_engine.context import (
    MarketState,
    SessionContext,
    SessionStatistics,
    SessionStatisticsQuality,
)
from app.market_engine.session_statistics import (
    SessionStatisticsAuthority,
    update_session_statistics,
)
from app.schemas.market_data import Instrument, ProviderSessionOhlc, Tick

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
_DATE = date(2026, 8, 10)
_NEXT_DATE = date(2026, 8, 11)
_T0 = datetime(2026, 8, 10, 9, 16, tzinfo=UTC)
_T1 = datetime(2026, 8, 10, 9, 20, tzinfo=UTC)
_T2 = datetime(2026, 8, 10, 9, 25, tzinfo=UTC)
_VERIFIED = SessionStatisticsAuthority(tick_aggregate_verified=True)  # tick-carried aggregate path
_UNVERIFIED = SessionStatisticsAuthority()


def _tick(
    *,
    at: datetime = _T1,
    open_: str = "100",
    high: str = "105",
    low: str = "98",
    close: str = "101",
    instrument: Instrument = _INSTRUMENT,
    with_ohlc: bool = True,
) -> Tick:
    ohlc = (
        ProviderSessionOhlc(
            open_price=Decimal(open_),
            high_price=Decimal(high),
            low_price=Decimal(low),
            close_price=Decimal(close),
        )
        if with_ohlc
        else None
    )
    return Tick(
        instrument=instrument, event_timestamp=at, last_price=Decimal("100"), session_ohlc=ohlc
    )


def _session(state: MarketState = MarketState.LIVE_SESSION, day: date = _DATE) -> SessionContext:
    return SessionContext(trading_date=day, market_state=state, exchange_timezone="Asia/Kolkata")


def _stats(
    *, open_: str = "100", high: str = "105", low: str = "98", at: datetime = _T1, day: date = _DATE
) -> SessionStatistics:
    return SessionStatistics(
        trading_date=day,
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        quality=SessionStatisticsQuality.AUTHORITATIVE,
        as_of=at,
    )


def _update(
    previous: SessionStatistics | None,
    *,
    tick: Tick | None = None,
    session: SessionContext | None = None,
    authority: SessionStatisticsAuthority = _VERIFIED,
) -> SessionStatistics | None:
    return update_session_statistics(
        tick=tick if tick is not None else _tick(),
        session=session if session is not None else _session(),
        previous=previous,
        authority=authority,
    )


# --------------------------------------------------------------------------- #
# Establishment and the authority gate
# --------------------------------------------------------------------------- #
def test_first_live_verified_aggregate_becomes_authoritative() -> None:
    stats = _update(None)
    assert stats is not None and stats.quality is SessionStatisticsQuality.AUTHORITATIVE
    assert stats.open_price == Decimal("100")
    assert stats.high_price == Decimal("105")
    assert stats.low_price == Decimal("98")
    assert stats.trading_date == _DATE
    assert stats.as_of == _T1


def test_valid_session_ohlc_cannot_become_authoritative_before_provider_verification() -> None:
    assert _update(None, authority=_UNVERIFIED) is None


def test_authority_disabled_retains_prior_when_present() -> None:
    prior = _stats()
    assert _update(prior, authority=_UNVERIFIED) is prior


# --------------------------------------------------------------------------- #
# Session-phase eligibility
# --------------------------------------------------------------------------- #
def test_pre_open_aggregate_is_ignored() -> None:
    assert _update(None, session=_session(MarketState.PRE_OPEN)) is None


def test_opening_auction_aggregate_is_ignored() -> None:
    assert _update(None, session=_session(MarketState.OPENING_AUCTION)) is None


def test_holiday_aggregate_is_ignored() -> None:
    assert _update(None, session=_session(MarketState.HOLIDAY)) is None


def test_closing_session_retains_prior_and_does_not_progress() -> None:
    prior = _stats(high="105")
    result = _update(
        prior, tick=_tick(at=_T2, high="108"), session=_session(MarketState.CLOSING_SESSION)
    )
    assert result is prior  # regular-session trading is LIVE_SESSION only


def test_market_closed_retains_prior() -> None:
    prior = _stats()
    assert _update(prior, session=_session(MarketState.MARKET_CLOSED)) is prior


def test_emergency_halt_retains_prior_unchanged() -> None:
    prior = _stats(high="105")
    result = _update(
        prior, tick=_tick(at=_T2, high="200"), session=_session(MarketState.EMERGENCY_HALT)
    )
    assert result is prior  # transport activity during a halt is not market progression


# --------------------------------------------------------------------------- #
# Missing aggregate
# --------------------------------------------------------------------------- #
def test_no_aggregate_and_no_prior_is_unavailable() -> None:
    assert _update(None, tick=_tick(with_ohlc=False)) is None


def test_no_aggregate_retains_prior_authoritative() -> None:
    prior = _stats()
    assert _update(prior, tick=_tick(at=_T2, with_ohlc=False)) is prior


# --------------------------------------------------------------------------- #
# Progression, reuse, and fail-closed reconciliation
# --------------------------------------------------------------------------- #
def test_identical_aggregate_reuses_prior_without_churn() -> None:
    prior = _stats(open_="100", high="105", low="98", at=_T1)
    result = _update(prior, tick=_tick(at=_T2, open_="100", high="105", low="98"))
    assert result is prior


def test_newer_high_is_accepted_from_the_whole_snapshot() -> None:
    prior = _stats(high="105", low="98")
    result = _update(prior, tick=_tick(at=_T2, high="108", low="98"))
    assert result is not None and result.high_price == Decimal("108")
    assert result.open_price == Decimal("100") and result.low_price == Decimal("98")


def test_newer_low_is_accepted() -> None:
    prior = _stats(high="105", low="98")
    result = _update(prior, tick=_tick(at=_T2, high="105", low="95"))
    assert result is not None and result.low_price == Decimal("95")


def test_high_and_low_progression_is_accepted_wholesale() -> None:
    prior = _stats(high="105", low="98")
    result = _update(prior, tick=_tick(at=_T2, open_="100", high="108", low="95"))
    assert result is not None
    assert (result.open_price, result.high_price, result.low_price) == (
        Decimal("100"),
        Decimal("108"),
        Decimal("95"),
    )


def test_stale_as_of_is_ignored() -> None:
    prior = _stats(at=_T1)
    result = _update(prior, tick=_tick(at=_T0, high="108"))
    assert result is prior


def test_high_regression_is_ignored() -> None:
    prior = _stats(high="105", low="98")
    assert _update(prior, tick=_tick(at=_T2, high="104", low="98")) is prior


def test_low_regression_is_ignored() -> None:
    prior = _stats(high="105", low="98")
    assert _update(prior, tick=_tick(at=_T2, high="105", low="99")) is prior


def test_open_change_is_ignored_and_never_merged() -> None:
    prior = _stats(open_="100", high="105", low="98")
    result = _update(prior, tick=_tick(at=_T2, open_="101", high="108", low="97"))
    assert result is prior  # no hybrid: corrected open never merges with prior extrema


# --------------------------------------------------------------------------- #
# Trading-date reset
# --------------------------------------------------------------------------- #
def test_new_trading_date_resets_and_creates_a_fresh_snapshot() -> None:
    prior = _stats(open_="100", high="105", low="98", day=_DATE)
    fresh = _update(
        prior,
        tick=_tick(at=_T2, open_="200", high="205", low="198", close="200"),
        session=_session(day=_NEXT_DATE),
    )
    assert fresh is not None and fresh.trading_date == _NEXT_DATE
    assert (fresh.open_price, fresh.high_price, fresh.low_price) == (
        Decimal("200"),
        Decimal("205"),
        Decimal("198"),
    )  # no previous-day leakage


def test_new_trading_date_without_live_phase_is_unavailable() -> None:
    prior = _stats(day=_DATE)
    assert _update(prior, session=_session(MarketState.PRE_OPEN, day=_NEXT_DATE)) is None


# --------------------------------------------------------------------------- #
# Isolation and determinism
# --------------------------------------------------------------------------- #
def test_updates_are_isolated_per_instrument() -> None:
    a_prior = _stats(open_="100", high="105", low="98")
    b_prior = _stats(open_="200", high="210", low="195")
    a_next = _update(a_prior, tick=_tick(at=_T2, high="108"))
    assert a_next is not None and a_next.high_price == Decimal("108")
    # B's state is a separate immutable value, untouched by A's update.
    assert b_prior.open_price == Decimal("200") and b_prior.high_price == Decimal("210")
    assert b_prior is not a_next


def test_same_sequence_is_deterministic() -> None:
    def run() -> list[SessionStatistics | None]:
        results: list[SessionStatistics | None] = []
        state: SessionStatistics | None = None
        for tick in (
            _tick(at=_T0, high="105", low="98"),
            _tick(at=_T1, high="108", low="97"),
            _tick(at=_T2, high="108", low="97"),
        ):
            state = update_session_statistics(
                tick=tick, session=_session(), previous=state, authority=_VERIFIED
            )
            results.append(state)
        return results

    assert run() == run()
