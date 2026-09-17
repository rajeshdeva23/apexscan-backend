"""Market-authority diagnostics endpoint (DECOUPLING PHASE H9C-P4, Gate K).

Exercises ``GET /api/v1/diagnostics/market-authority`` against a real disposable ``redislite``
server wired through the ``get_redis`` dependency. Proves it is READ-ONLY (never creates/mutates the
ownership lease), secret-free, fail-closed (missing/stale evidence → not ready), and never touches
Dhan or acquires ownership.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.cache import get_redis
from app.core.lifecycle import ApplicationLifecycle
from app.main import create_app
from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.loss_detection import LossDetectionResult, LossDetectionState
from app.market_ipc.state import IngestionHealthState, health_key
from app.schemas.market_data import ProviderStatus

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 17, 10, 15, 30, tzinfo=UTC)
_OWNER_KEY = "md:provider:ownership"
_MINT_KEY = "md:provider:token:mint"
_ENDPOINT = "/api/v1/diagnostics/market-authority"


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def redis(redis_socket: str) -> Redis:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


class _HealthyDep:
    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.verify_connectivity = AsyncMock()
        self.dispose = AsyncMock()
        self.close = AsyncMock()


def _app(redis_socket: str, *, runtime: object | None = None) -> object:
    app = create_app(lifecycle=ApplicationLifecycle(_HealthyDep(), _HealthyDep(), provider=None))

    async def _yield_redis():  # noqa: ANN202 - test dependency override
        client: Redis = Redis(unix_socket_path=redis_socket)
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_redis] = _yield_redis
    app.state.market_consumer_runtime = runtime
    return app


async def _get(app: object, path: str = _ENDPOINT):  # noqa: ANN202
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def _seed_ownership(redis: Redis) -> None:
    await redis.set(
        _OWNER_KEY,
        json.dumps(
            {
                "owner_role": "ingestion",
                "instance_id": "inst-xyz",
                "fencing_generation": 5,
                "acquired_at_ms": 1_700_000_000_000,
            }
        ),
    )


async def _seed_health(redis: Redis) -> None:
    state = IngestionHealthState(
        producer_id="market-ingestion",
        producer_epoch=5,
        updated_at=_NOW,
        ingestion=ProviderStatus.HEALTHY,
        transport=ProviderStatus.HEALTHY,
        universe_sync=ProviderStatus.UNKNOWN,
        last_published_sequence=99,
        terminal_publication_break=False,
        publication_outcome_uncertain=False,
    )
    await redis.set(health_key(MarketIpcConfig()), state.model_dump_json())


class _InsufficientRuntime:
    async def evaluate_authority_readiness(self) -> LossDetectionResult:
        return LossDetectionResult(
            state=LossDetectionState.INSUFFICIENT_EVIDENCE,
            reason="producer md:health snapshot is missing or stale",
            ready_for_authority=False,
            producer_id="unknown",
            producer_epoch=0,
            producer_last_published_sequence=None,
            consumer_last_applied_sequence=None,
            stream_length=0,
            stream_last_generated_id="0-0",
            group_last_delivered_id="0-0",
            pending=0,
        )


async def test_empty_redis_is_fail_closed_and_no_store(redis_socket: str, redis: Redis) -> None:
    resp = await _get(_app(redis_socket))
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    body = resp.json()
    assert body["ownership"]["has_owner"] is False
    assert body["token_mint"]["recorded"] is False
    assert body["ingestion_health"]["present"] is False
    assert body["ingestion_health"]["stale"] is True
    assert body["authority"]["ready"] is False  # no runtime composed → unknown
    assert body["authority"]["state"] == "unknown"


async def test_seeded_signals_are_projected(redis_socket: str, redis: Redis) -> None:
    await _seed_ownership(redis)
    await _seed_health(redis)
    await redis.set(
        _MINT_KEY,
        json.dumps(
            {
                "last_mint_at_ms": 1_700_000_000_000,
                "owner_role": "ingestion",
                "instance_id": "inst-xyz",
                "fencing_generation": 5,
            }
        ),
    )
    body = (await _get(_app(redis_socket))).json()
    assert body["ownership"]["has_owner"] is True
    assert body["ownership"]["owner_role"] == "ingestion"
    assert body["ownership"]["fencing_generation"] == 5
    assert body["token_mint"]["recorded"] is True
    assert body["ingestion_health"]["present"] is True
    assert body["ingestion_health"]["producer_epoch"] == 5
    assert body["ingestion_health"]["last_published_sequence"] == 99


async def test_endpoint_is_read_only_never_creates_or_mutates_ownership(
    redis_socket: str, redis: Redis
) -> None:
    # No ownership key exists → the read-only endpoint must not create one.
    assert await redis.exists(_OWNER_KEY) == 0
    await _get(_app(redis_socket))
    assert await redis.exists(_OWNER_KEY) == 0  # no acquire, no key created

    # An existing lease record is never mutated by a read.
    await _seed_ownership(redis)
    before = await redis.get(_OWNER_KEY)
    await _get(_app(redis_socket))
    assert await redis.get(_OWNER_KEY) == before


async def test_authority_readiness_from_runtime_is_surfaced(
    redis_socket: str, redis: Redis
) -> None:
    body = (await _get(_app(redis_socket, runtime=_InsufficientRuntime()))).json()
    assert body["authority"]["ready"] is False
    assert body["authority"]["state"] == "insufficient_evidence"


async def test_response_leaks_no_secret_fields(redis_socket: str, redis: Redis) -> None:
    await _seed_ownership(redis)
    await _seed_health(redis)
    text = (await _get(_app(redis_socket))).text.lower()
    for secret in ("secret", "totp", "password", "access_token", "client_id", "dhan_pin"):
        assert secret not in text


async def test_get_only_no_mutation_verbs(redis_socket: str, redis: Redis) -> None:
    transport = ASGITransport(app=_app(redis_socket))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post(_ENDPOINT)).status_code == 405
        assert (await client.delete(_ENDPOINT)).status_code == 405
