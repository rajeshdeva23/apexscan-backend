"""Offline dry-run for the corrected SECTOR-5B historical probe (R3.1).

No Dhan network: an autouse kill-switch makes any httpx/socket use fail the test, and the
probe runs against a mock adapter. Proves the corrected call order, the R3 fail-closed
failure mode, and the pure P3/P4/quality/artifact evaluators. Nothing here is provider
evidence — mocks never upgrade a prerequisite verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    InstrumentClass,
    MarketSegment,
)
from app.tools.sector_historical_probe import (
    IST,
    BarLabel,
    HistoricalProbeAdapter,
    classify_bar_label,
    expected_minute_starts,
    feature_eligible,
    resolve_instrument,
    run_probe,
    session_quality,
    write_raw_artifact,
)

RELIANCE = Instrument(
    exchange="NSE",
    symbol="RELIANCE",
    market_segment=MarketSegment.EQUITY,
    instrument_class=InstrumentClass.CASH,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # All Dhan I/O is httpx; blocking the client guarantees no real HTTP egress in this probe.
    def _blocked(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted in an offline R3.1 test")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _blocked)
    monkeypatch.setattr(httpx.Client, "__init__", _blocked)


def _candle(
    start: datetime, o: str = "100", h: str = "101", low: str = "99", c: str = "100"
) -> Candle:
    return Candle(
        instrument=RELIANCE,
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=1),
        open_price=Decimal(o),
        high_price=Decimal(h),
        low_price=Decimal(low),
        close_price=Decimal(c),
        traded_quantity=10,
    )


class _MockAdapter:
    """Records call order; serves fixtures. No network."""

    def __init__(self, instruments: tuple[Instrument, ...], candles: tuple[Candle, ...]) -> None:
        self._instruments = instruments
        self._candles = candles
        self.calls: list[str] = []
        self.last_request: HistoricalRequest | None = None

    async def connect(self) -> None:
        self.calls.append("connect")

    async def load_instruments(self) -> tuple[Instrument, ...]:
        self.calls.append("load_instruments")
        return self._instruments

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        self.calls.append("load_historical_data")
        self.last_request = request
        return HistoricalResult(request=request, candles=self._candles)


def test_network_kill_switch_active() -> None:
    with pytest.raises(AssertionError):
        httpx.AsyncClient()


async def test_positive_master_loaded_reaches_historical_boundary() -> None:
    start = datetime(2026, 8, 29, 3, 45, tzinfo=UTC)
    adapter: HistoricalProbeAdapter = _MockAdapter((RELIANCE,), (_candle(start),))
    result = await run_probe(
        adapter, symbol="RELIANCE", start_utc=start, end_utc=start + timedelta(hours=6, minutes=15)
    )
    assert isinstance(result, HistoricalResult)
    # corrected order: load_instruments BEFORE load_historical_data
    assert adapter.calls == ["connect", "load_instruments", "load_historical_data"]  # type: ignore[attr-defined]
    assert adapter.last_request.instrument == RELIANCE  # type: ignore[attr-defined,union-attr]
    assert adapter.last_request.interval == timedelta(minutes=1)  # type: ignore[attr-defined,union-attr]


async def test_negative_without_master_fails_closed() -> None:
    # R3 failure mode: master not loaded -> symbol cannot resolve -> fail closed, no historical call
    adapter = _MockAdapter((), (_candle(datetime(2026, 8, 29, 3, 45, tzinfo=UTC)),))
    with pytest.raises(LookupError):
        await run_probe(
            adapter,
            symbol="RELIANCE",
            start_utc=datetime(2026, 8, 29, 3, 45, tzinfo=UTC),
            end_utc=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        )
    assert "load_historical_data" not in adapter.calls


def test_resolve_instrument_fail_closed_on_ambiguity() -> None:
    assert resolve_instrument((RELIANCE,), "RELIANCE") == RELIANCE
    assert resolve_instrument((), "RELIANCE") is None
    assert resolve_instrument((RELIANCE, RELIANCE), "RELIANCE") is None  # not unique


def test_classify_bar_label_no_left_assumption() -> None:
    open_ist = datetime(2026, 8, 29, 9, 15, tzinfo=IST)
    close_ist = datetime(2026, 8, 29, 15, 30, tzinfo=IST)
    # left-labeled: first start == open, last end == close
    assert (
        classify_bar_label(
            first_start_utc=open_ist,
            last_end_utc=close_ist,
            bar_count=375,
            session_open_ist=open_ist,
            session_close_ist=close_ist,
        )
        is BarLabel.LEFT_LABEL
    )
    # right-labeled: both shifted by one interval
    assert (
        classify_bar_label(
            first_start_utc=open_ist + timedelta(minutes=1),
            last_end_utc=close_ist + timedelta(minutes=1),
            bar_count=375,
            session_open_ist=open_ist,
            session_close_ist=close_ist,
        )
        is BarLabel.RIGHT_LABEL
    )
    # only first matches, last doesn't -> AMBIGUOUS (a lone 09:15 is not proof)
    assert (
        classify_bar_label(
            first_start_utc=open_ist,
            last_end_utc=close_ist - timedelta(minutes=5),
            bar_count=370,
            session_open_ist=open_ist,
            session_close_ist=close_ist,
        )
        is BarLabel.AMBIGUOUS
    )
    assert (
        classify_bar_label(
            first_start_utc=open_ist,
            last_end_utc=close_ist,
            bar_count=0,
            session_open_ist=open_ist,
            session_close_ist=close_ist,
        )
        is BarLabel.INVALID
    )


def test_feature_eligible_0930_boundary() -> None:
    eval_t = datetime(2026, 8, 29, 9, 30, tzinfo=IST)
    last_eligible_end = datetime(2026, 8, 29, 9, 30, tzinfo=IST)  # [09:29,09:30)
    first_forbidden_end = datetime(2026, 8, 29, 9, 31, tzinfo=IST)  # [09:30,09:31)
    assert feature_eligible(last_eligible_end, eval_t) is True
    assert feature_eligible(first_forbidden_end, eval_t) is False


def test_expected_minute_starts_and_quality() -> None:
    s = datetime(2026, 8, 29, 3, 45, tzinfo=UTC)
    e = s + timedelta(minutes=5)
    starts = expected_minute_starts(s, e)
    assert len(starts) == 5
    candles = [
        _candle(s),
        _candle(s + timedelta(minutes=1)),
        _candle(s + timedelta(minutes=1)),
    ]  # dup
    bad = Candle(
        instrument=RELIANCE,
        start_timestamp=s + timedelta(minutes=2),
        end_timestamp=s + timedelta(minutes=3),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=1,
    )
    q = session_quality([*candles, bad], starts, s, e)
    assert q.expected == 5 and q.duplicates == 1
    assert s + timedelta(minutes=3) in q.missing and s + timedelta(minutes=4) in q.missing


def test_write_raw_artifact_hashes_and_rejects_secrets(tmp_path) -> None:  # type: ignore[no-untyped-def]
    s = datetime(2026, 8, 29, 3, 45, tzinfo=UTC)
    digest = write_raw_artifact(
        tmp_path / "raw.json", (_candle(s),), {"symbol": "RELIANCE", "interval": "1m"}
    )
    assert len(digest) == 64 and (tmp_path / "raw.json").exists()
    with pytest.raises(ValueError, match="credential-like"):
        write_raw_artifact(tmp_path / "bad.json", (_candle(s),), {"access_token": "x"})
