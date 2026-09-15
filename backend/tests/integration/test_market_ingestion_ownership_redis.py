"""Cross-process single-Dhan-owner interlock over real Redis (PHASE H9A, ADR-030).

Proves the Redis lease + fencing coordinator against a disposable ``redislite`` server (never a
shared/production Redis): exactly one owner under contention, monotonic fencing, stale-owner
rejection on renew/release/validate, TTL expiry + re-acquire, fail-closed on Redis unavailability,
and offline BACKEND→NONE→INGESTION cutover + INGESTION→NONE→BACKEND rollback rehearsals in which the
number of concurrently-active fake providers never exceeds one. No real Dhan, no production, no
ownership transfer (that is the separately-governed H9B); no IPC authority.
"""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.market_ingestion.ownership import (
    OwnerRole,
    OwnershipLease,
    OwnershipLeaseConfig,
    RedisOwnershipCoordinator,
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


def _config(**overrides: object) -> OwnershipLeaseConfig:
    base = {"lease_ttl_seconds": 30, "renewal_interval_seconds": 10}
    base.update(overrides)
    return OwnershipLeaseConfig(**base)


def _coordinator(
    redis: Redis, config: OwnershipLeaseConfig | None = None
) -> RedisOwnershipCoordinator:
    return RedisOwnershipCoordinator(redis, config or _config())


class _FakeProvider:
    """Observable fake Dhan provider — start/stop only; never a real socket or auth."""

    def __init__(self) -> None:
        self.active = False
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.active = True
        self.start_count += 1

    def stop(self) -> None:
        self.active = False
        self.stop_count += 1


# =========================================================================== #
# T01/T02/T03: acquire / conflict / same-role different-incarnation
# =========================================================================== #
async def test_h9a_t01_t02_t03_acquire_and_conflict(redis: Redis) -> None:
    coord = _coordinator(redis)
    backend = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert backend is not None and backend.fencing_generation == 1

    # T02: a different role cannot acquire while backend holds a live lease.
    assert await coord.acquire(OwnerRole.INGESTION, "ingestion-1") is None
    # T03: the same role, a *different* incarnation, also cannot steal a live lease.
    assert await coord.acquire(OwnerRole.BACKEND, "backend-2") is None
    # idempotent: the exact same incarnation re-acquires, keeping its generation.
    again = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert again is not None and again.fencing_generation == 1


# =========================================================================== #
# T04-T09: renew / stale renew / release / stale release / validate / stale validate
# =========================================================================== #
async def test_h9a_t04_to_t09_renew_release_validate_and_stale(redis: Redis) -> None:
    coord = _coordinator(redis)
    a = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert a is not None
    assert await coord.renew(a) is True  # T04 current owner renews
    assert await coord.validate(a) is True  # T08 current lease validates
    assert await coord.release(a) is True  # T06 current owner releases

    b = await coord.acquire(OwnerRole.INGESTION, "ingestion-1")  # T11 new owner, higher fence
    assert b is not None and b.fencing_generation == 2
    assert await coord.renew(a) is False  # T05 stale renew rejected
    assert await coord.release(a) is False  # T07 stale release cannot remove new owner
    assert await coord.validate(a) is False  # T09 stale validation false
    assert await coord.validate(b) is True
    # the stale release did NOT delete b's lease:
    assert (await coord.snapshot()).instance_id == "ingestion-1"


# =========================================================================== #
# T10/I03: TTL expiry lets a new owner acquire with a higher fence
# =========================================================================== #
async def test_h9a_t10_i03_expiry_permits_new_owner(redis: Redis) -> None:
    coord = _coordinator(redis, _config(lease_ttl_seconds=2, renewal_interval_seconds=1))
    a = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert a is not None and a.fencing_generation == 1
    # Simulate a crash (no renew, no release). Before expiry, nobody else can acquire.
    assert await coord.acquire(OwnerRole.INGESTION, "ingestion-1") is None
    await asyncio.sleep(2.3)  # exceed the 2s lease TTL
    b = await coord.acquire(OwnerRole.INGESTION, "ingestion-1")
    assert b is not None and b.fencing_generation == 2  # higher fence after expiry
    assert await coord.validate(a) is False  # the crashed owner's lease is dead


# =========================================================================== #
# T12/T13/T14: fail closed on Redis unavailable (acquire / renew / validate)
# =========================================================================== #
async def test_h9a_t12_t13_t14_redis_unavailable_fails_closed() -> None:
    down: Redis = Redis(unix_socket_path="/nonexistent/ownership-down.sock")
    coord = _coordinator(down)
    lease = OwnershipLease(OwnerRole.BACKEND, "backend-1", 1)
    assert await coord.acquire(OwnerRole.BACKEND, "backend-1") is None  # never grants ownership
    assert await coord.renew(lease) is False
    assert await coord.validate(lease) is False
    assert (await coord.snapshot()).has_owner is False  # read fails closed to no-owner
    await down.aclose()


# =========================================================================== #
# T15: bounded diagnostics reflect activity without secrets
# =========================================================================== #
async def test_h9a_t15_diagnostics_bounded(redis: Redis) -> None:
    coord = _coordinator(redis)
    a = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert a is not None
    await coord.acquire(OwnerRole.INGESTION, "ingestion-1")  # conflict
    await coord.renew(a)
    snap = await coord.snapshot()
    assert snap.owner_role == "backend" and snap.fencing_generation == 1
    assert snap.acquire_success_total == 1
    assert snap.acquire_conflict_total == 1
    assert snap.renew_success_total == 1


# =========================================================================== #
# I01: backend vs ingestion race — exactly one winner
# =========================================================================== #
async def test_h9a_i01_backend_vs_ingestion_race_single_winner(redis: Redis) -> None:
    coord = _coordinator(redis)
    results = await asyncio.gather(
        coord.acquire(OwnerRole.BACKEND, "backend-1"),
        coord.acquire(OwnerRole.INGESTION, "ingestion-1"),
    )
    assert sum(1 for r in results if r is not None) == 1


# =========================================================================== #
# I02: two ingestion incarnations race — exactly one winner
# =========================================================================== #
async def test_h9a_i02_same_role_race_single_winner(redis: Redis) -> None:
    coord = _coordinator(redis)
    results = await asyncio.gather(
        coord.acquire(OwnerRole.INGESTION, "ingestion-a"),
        coord.acquire(OwnerRole.INGESTION, "ingestion-b"),
    )
    assert sum(1 for r in results if r is not None) == 1


# =========================================================================== #
# I08: renewal preserves ownership; a contender still cannot acquire
# =========================================================================== #
async def test_h9a_i08_renewal_preserves_ownership(redis: Redis) -> None:
    coord = _coordinator(redis)
    a = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert a is not None
    for _ in range(5):
        assert await coord.renew(a) is True
        assert await coord.acquire(OwnerRole.INGESTION, "ingestion-1") is None


# =========================================================================== #
# §49 contention stress: many races, never two winners
# =========================================================================== #
async def test_h9a_contention_stress_never_two_owners(redis: Redis) -> None:
    coord = _coordinator(redis)
    for round_no in range(1_000):
        await redis.delete(coord._config.owner_key)  # fresh contended key each round
        winners = await asyncio.gather(
            coord.acquire(OwnerRole.BACKEND, f"backend-{round_no}"),
            coord.acquire(OwnerRole.INGESTION, f"ingestion-{round_no}"),
            coord.acquire(OwnerRole.BACKEND, f"backend-alt-{round_no}"),
        )
        assert sum(1 for w in winners if w is not None) == 1  # never two/three


# =========================================================================== #
# §50 fencing stress: generations strictly increase, never reused
# =========================================================================== #
async def test_h9a_fencing_stress_monotonic(redis: Redis) -> None:
    coord = _coordinator(redis)
    generations: list[int] = []
    for i in range(1_000):
        lease = await coord.acquire(OwnerRole.INGESTION, f"ingestion-{i}")
        assert lease is not None
        generations.append(lease.fencing_generation)
        await coord.release(lease)
    assert generations == sorted(generations)  # monotonic
    assert len(set(generations)) == len(generations)  # never reused
    assert generations[0] == 1 and generations[-1] == 1_000  # contiguous under a live Redis


# =========================================================================== #
# §51 offline cutover rehearsal: BACKEND -> NONE -> INGESTION, max one provider active
# =========================================================================== #
async def test_h9a_offline_cutover_rehearsal_single_owner(redis: Redis) -> None:
    coord = _coordinator(redis)
    backend_provider, ingestion_provider = _FakeProvider(), _FakeProvider()

    def concurrent_active() -> int:
        return int(backend_provider.active) + int(ingestion_provider.active)

    # Initial: backend owns and its provider is active (ownership precedes provider).
    backend_lease = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert backend_lease is not None
    backend_provider.start()
    peak = concurrent_active()

    backend_provider.stop()  # 1) stop legacy provider intake FIRST
    peak = max(peak, concurrent_active())
    assert await coord.release(backend_lease) is True  # 2) release ownership
    peak = max(peak, concurrent_active())
    assert (await coord.snapshot()).has_owner is False  # 3) NONE

    ingestion_lease = await coord.acquire(OwnerRole.INGESTION, "ingestion-1")  # 4) acquire
    assert ingestion_lease is not None
    assert await coord.validate(ingestion_lease) is True
    ingestion_provider.start()  # 5) only now start the new provider
    peak = max(peak, concurrent_active())

    assert peak == 1  # MAX_CONCURRENT_PROVIDER_OWNERS == 1 (never both active)
    assert ingestion_lease.fencing_generation > backend_lease.fencing_generation
    assert backend_provider.active is False and ingestion_provider.active is True


# =========================================================================== #
# §52 offline rollback rehearsal: INGESTION -> NONE -> BACKEND, max one provider active
# =========================================================================== #
async def test_h9a_offline_rollback_rehearsal_single_owner(redis: Redis) -> None:
    coord = _coordinator(redis)
    backend_provider, ingestion_provider = _FakeProvider(), _FakeProvider()

    def concurrent_active() -> int:
        return int(backend_provider.active) + int(ingestion_provider.active)

    ingestion_lease = await coord.acquire(OwnerRole.INGESTION, "ingestion-1")
    assert ingestion_lease is not None
    ingestion_provider.start()
    peak = concurrent_active()

    ingestion_provider.stop()
    peak = max(peak, concurrent_active())
    assert await coord.release(ingestion_lease) is True
    assert (await coord.snapshot()).has_owner is False

    backend_lease = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert backend_lease is not None
    backend_provider.start()
    peak = max(peak, concurrent_active())

    assert peak == 1
    assert backend_provider.active is True and ingestion_provider.active is False


# =========================================================================== #
# §34 cutover failure: new owner cannot acquire -> stays NONE, no dual ownership
# =========================================================================== #
async def test_h9a_cutover_halt_when_new_owner_cannot_acquire(redis: Redis) -> None:
    coord = _coordinator(redis)
    backend_provider = _FakeProvider()
    backend_lease = await coord.acquire(OwnerRole.BACKEND, "backend-1")
    assert backend_lease is not None
    backend_provider.start()
    backend_provider.stop()
    await coord.release(backend_lease)

    # A stray holder grabs ownership before ingestion tries (models contention at cutover).
    intruder = await coord.acquire(OwnerRole.INGESTION, "ingestion-other")
    assert intruder is not None
    ingestion_lease = await coord.acquire(OwnerRole.INGESTION, "ingestion-1")
    assert ingestion_lease is None  # cannot acquire -> cutover halts SAFE
    # Backend was NOT auto-restarted; exactly one owner exists, no dual ownership.
    assert backend_provider.active is False
    assert (await coord.snapshot()).instance_id == "ingestion-other"
