"""Cross-process Dhan token-mint throttle (DECOUPLING PHASE H9C-P3, Gate H).

Proves the persisted mint reservation against a real disposable ``redislite`` server: a first mint
is allowed, a second inside the cooldown is denied cross-process (survives a "restart" = a fresh
guard on the same Redis), it clears once the window elapses, and it fails CLOSED (no mint) on a
Redis outage or a malformed durable state. The record is metadata only — never the access token.
"""

from __future__ import annotations

import json

import pytest
from redis.asyncio import Redis

from app.market_ingestion.ownership import OwnerRole, OwnershipLease
from app.market_ingestion.token_mint_guard import (
    RedisTokenMintGuard,
    TokenMintConfig,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")


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


def _config(cooldown: int = 120) -> TokenMintConfig:
    return TokenMintConfig(cooldown_seconds=cooldown)


def _lease(
    role: OwnerRole = OwnerRole.BACKEND, instance: str = "a", fence: int = 1
) -> OwnershipLease:
    return OwnershipLease(owner_role=role, instance_id=instance, fencing_generation=fence)


async def test_first_mint_is_allowed(redis: Redis) -> None:
    guard = RedisTokenMintGuard(redis, _config())
    decision = await guard.reserve_mint(_lease())
    assert decision.allowed is True
    assert decision.remaining_seconds == 0.0


async def test_second_mint_inside_cooldown_is_denied(redis: Redis) -> None:
    guard = RedisTokenMintGuard(redis, _config(cooldown=120))
    assert (await guard.reserve_mint(_lease())).allowed is True
    denied = await guard.reserve_mint(_lease())
    assert denied.allowed is False
    assert 0 < denied.remaining_seconds <= 120


async def test_cooldown_is_enforced_across_processes_and_owners(redis: Redis) -> None:
    # Owner A mints; owner B (a fresh guard = a different process/incarnation) sees the cooldown.
    guard_a = RedisTokenMintGuard(redis, _config())
    guard_b = RedisTokenMintGuard(redis, _config())
    assert (await guard_a.reserve_mint(_lease(OwnerRole.BACKEND, "a", 1))).allowed is True
    handoff = await guard_b.reserve_mint(_lease(OwnerRole.INGESTION, "b", 2))
    assert handoff.allowed is False  # successor cannot mint immediately after the predecessor


async def test_mint_allowed_again_after_the_window_elapses(redis: Redis) -> None:
    guard = RedisTokenMintGuard(redis, _config())
    assert (await guard.reserve_mint(_lease())).allowed is True
    # The record self-expires at the cooldown; simulate the window elapsing by clearing it.
    await redis.delete(TokenMintConfig().mint_key)
    assert (await guard.reserve_mint(_lease())).allowed is True


async def test_reservation_survives_a_restart(redis: Redis) -> None:
    RedisTokenMintGuard(redis, _config())  # (constructing does nothing durable)
    minted = await RedisTokenMintGuard(redis, _config()).reserve_mint(_lease())
    assert minted.allowed is True
    # A brand-new guard (process restart) over the SAME Redis still sees the cooldown.
    restarted = RedisTokenMintGuard(redis, _config())
    assert (await restarted.reserve_mint(_lease())).allowed is False


async def test_record_is_metadata_only_never_the_token(redis: Redis) -> None:
    guard = RedisTokenMintGuard(redis, _config())
    await guard.reserve_mint(_lease(OwnerRole.BACKEND, "inst-x", 7))
    raw = await redis.get(TokenMintConfig().mint_key)
    record = json.loads(raw)
    assert set(record) == {"last_mint_at_ms", "owner_role", "instance_id", "fencing_generation"}
    assert record["owner_role"] == "backend"
    assert record["instance_id"] == "inst-x"
    assert record["fencing_generation"] == 7
    # No token/credential fields anywhere in the persisted metadata.
    for key in record:
        assert not any(s in key.lower() for s in ("token", "secret", "pin", "totp", "access"))


async def test_redis_failure_fails_closed() -> None:
    unreachable: Redis = Redis(unix_socket_path="/nonexistent/apexscan-h9cp3.socket")
    guard = RedisTokenMintGuard(unreachable, _config())
    decision = await guard.reserve_mint(_lease())
    assert decision.allowed is False  # a Redis outage is never read as permission to mint
    assert decision.remaining_seconds == 120.0  # full cooldown reported (fail closed)
    await unreachable.aclose()


async def test_malformed_state_fails_closed(redis: Redis) -> None:
    await redis.set(TokenMintConfig().mint_key, b"not-json")
    guard = RedisTokenMintGuard(redis, _config())
    decision = await guard.reserve_mint(_lease())
    assert decision.allowed is False  # a corrupt record is never read as permission to mint
