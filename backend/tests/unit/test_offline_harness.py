"""Offline validation-harness tests.

Prove the dev-only offline composition produces a genuine Narrow CPR snapshot
through the real runtime pipeline over HTTP — no PostgreSQL, Redis, or Dhan — and
that its offline doubles behave as intended. This is the automated counterpart to
the local UI acceptance run.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.schemas.market_data import HistoricalRequest
from app.services.offline_harness.app_factory import create_offline_app
from app.services.offline_harness.fixture_provider import (
    DEFAULT_UNAVAILABLE,
    REFERENCE_INSTANT,
    SYMBOLS,
    UNIVERSE_SIZE,
    OfflineFixtureProvider,
    _close_for,
    _instrument,
)
from app.services.offline_harness.in_memory_lifecycles import (
    InMemoryDatabaseLifecycle,
    InMemoryRedisLifecycle,
)


def _session_request(symbol: str) -> HistoricalRequest:
    return HistoricalRequest(
        instrument=_instrument(symbol),
        start_timestamp=REFERENCE_INSTANT,
        end_timestamp=REFERENCE_INSTANT + timedelta(days=1),
        interval=timedelta(days=1),
    )


async def test_in_memory_lifecycles_are_verified_noops() -> None:
    database = InMemoryDatabaseLifecycle()
    redis = InMemoryRedisLifecycle()
    await database.initialize("postgresql+asyncpg://x/y", echo=True)
    await database.verify_connectivity()
    await database.dispose()
    await redis.initialize("redis://x")
    await redis.verify_connectivity()
    await redis.close()


def test_fixture_universe_is_the_synthetic_208() -> None:
    universe = OfflineFixtureProvider().load_nse_cash_equity_live_universe()
    assert len(universe.cash_references) == UNIVERSE_SIZE == 208
    assert tuple(ref.instrument.symbol for ref in universe.cash_references) == SYMBOLS


async def test_fixture_history_available_matches_pivot_identity() -> None:
    result = await OfflineFixtureProvider().load_historical_data(_session_request("SYM000"))
    assert len(result.candles) == 1
    candle = result.candles[0]
    assert candle.high_price + candle.low_price + candle.close_price == Decimal("300")
    assert candle.close_price == _close_for("SYM000")


async def test_fixture_history_unavailable_returns_no_candles() -> None:
    unavailable_symbol = next(iter(DEFAULT_UNAVAILABLE))
    result = await OfflineFixtureProvider().load_historical_data(
        _session_request(unavailable_symbol)
    )
    assert result.candles == ()


async def test_offline_harness_serves_a_real_partial_snapshot() -> None:
    app = create_offline_app()
    lifecycle = app.state.lifecycle
    await lifecycle.start(get_settings())
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://offline") as client:
            body = await _poll_until_ranked(client, expected_eligible=205)
            headers = (await client.get("/api/v1/scanners/narrow_cpr")).headers
            assert headers["cache-control"] == "no-store"
        snapshot = body["snapshot"]
        assert snapshot["strategy_id"] == "narrow_cpr"
        assert snapshot["expected_count"] == 208
        assert snapshot["evaluated_count"] == 205
        assert snapshot["eligible_count"] == 205
        assert snapshot["completeness"] == "partial"
        candidates = snapshot["candidates"]
        assert len(candidates) == 205
        assert [c["rank"] for c in candidates] == list(range(1, 206))
        assert [c["symbol"] for c in candidates] == [
            s for s in SYMBOLS if s not in DEFAULT_UNAVAILABLE
        ]
        assert candidates[0]["ranking_metric_name"] == "cpr_width_pct"
        assert all(isinstance(c["ranking_metric_value"], str) for c in candidates)
        widths = [Decimal(c["ranking_metric_value"]) for c in candidates]
        assert widths[0] == Decimal("0.1")
        assert widths == sorted(widths)
    finally:
        await lifecycle.shutdown()


async def _poll_until_ranked(client: AsyncClient, *, expected_eligible: int) -> dict:
    for _ in range(5000):
        response = await client.get("/api/v1/scanners/narrow_cpr")
        assert response.status_code == 200
        body = response.json()
        snapshot = body["snapshot"]
        if snapshot is not None and snapshot["eligible_count"] >= expected_eligible:
            return body
        await asyncio.sleep(0)
    raise AssertionError("offline scanner did not reach the expected ranked count in time")
