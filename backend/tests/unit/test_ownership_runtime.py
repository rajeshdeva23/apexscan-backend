"""ProviderOwnershipGuard orchestration around the H9A lease primitive (DECOUPLING PHASE H9B).

Deterministic, offline, no Redis: the guard drives a FAKE coordinator that faithfully mimics the
H9A fenced-lease contract (idempotent-or-conflict acquire, fenced renew/release/validate,
monotonic generation). Renewal is stepped through an injected permit sleeper, so correctness is
asserted on observed transitions — never on elapsed wall-time. Proves per-incarnation identity,
acquire→validate, renewal, ownership loss on a failed renewal, stale-owner rejection, and the
release discipline (only a still-owned lease; never a lost one).
"""

from __future__ import annotations

import asyncio

import pytest

from app.market_ingestion.ownership import OwnerRole, OwnershipLease, OwnershipState
from app.market_ingestion.ownership_runtime import (
    OwnershipAcquisitionError,
    ProviderOwnershipGuard,
)


class _FakeCoordinator:
    """In-memory stand-in for RedisOwnershipCoordinator honouring the H9A fencing contract."""

    def __init__(self) -> None:
        self._record: tuple[str, str, int] | None = None  # (role, instance, generation)
        self._fence = 0
        self.acquire_calls = 0
        self.renew_calls = 0
        self.release_calls = 0
        self.mark_lost_calls = 0
        self.renew_ok = True  # flip to simulate a lost/fenced-out lease on the next renew

    async def acquire(self, role: OwnerRole, instance_id: str) -> OwnershipLease | None:
        self.acquire_calls += 1
        if self._record is not None:
            r_role, r_instance, generation = self._record
            if r_role == role.value and r_instance == instance_id:
                return OwnershipLease(role, instance_id, generation)  # idempotent
            return None  # a different live owner holds it — conflict
        self._fence += 1
        self._record = (role.value, instance_id, self._fence)
        return OwnershipLease(role, instance_id, self._fence)

    async def renew(self, lease: OwnershipLease) -> bool:
        self.renew_calls += 1
        if not self.renew_ok:
            return False
        return self._matches(lease)

    async def release(self, lease: OwnershipLease) -> bool:
        self.release_calls += 1
        if self._matches(lease):
            self._record = None
            return True
        return False

    async def validate(self, lease: OwnershipLease) -> bool:
        return self._matches(lease)

    async def mark_ownership_lost(self) -> None:
        self.mark_lost_calls += 1

    def evict(self) -> None:
        """Simulate the record vanishing (TTL expiry / a newer owner fenced it out)."""
        self._record = None

    def _matches(self, lease: OwnershipLease) -> bool:
        return self._record is not None and self._record[1:] == (
            lease.instance_id,
            lease.fencing_generation,
        )


class _PermitSleeper:
    """A renewal sleeper that blocks until the test grants a permit (deterministic stepping)."""

    def __init__(self) -> None:
        self.calls = 0
        self._permits: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, _seconds: float) -> None:
        self.calls += 1
        await self._permits.get()

    def grant(self, count: int = 1) -> None:
        for _ in range(count):
            self._permits.put_nowait(None)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _guard(
    coord: _FakeCoordinator, sleeper: _PermitSleeper, **kwargs: object
) -> ProviderOwnershipGuard:
    return ProviderOwnershipGuard(
        coordinator=coord,  # type: ignore[arg-type]
        role=OwnerRole.INGESTION,
        renewal_interval_seconds=10.0,
        sleep=sleeper,
        **kwargs,  # type: ignore[arg-type]
    )


# =========================================================================== #
# Identity & incarnation semantics
# =========================================================================== #
def test_instance_id_is_unique_per_guard_and_stable_within_one() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    a = _guard(coord, sleeper)
    b = _guard(coord, sleeper)
    assert a.instance_id != b.instance_id  # two incarnations → two identities
    assert a.instance_id == a.instance_id  # stable within one incarnation
    assert len(a.instance_id) == 32  # uuid4().hex


def test_explicit_instance_id_is_respected() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    guard = _guard(coord, sleeper, instance_id="ingestion-fixed")
    assert guard.instance_id == "ingestion-fixed"


# =========================================================================== #
# acquire → validate
# =========================================================================== #
async def test_acquire_or_fail_takes_ownership_and_fences() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    guard = _guard(coord, sleeper)
    lease = await guard.acquire_or_fail()
    assert guard.state is OwnershipState.OWNER
    assert lease.fencing_generation == 1
    assert guard.fencing_generation == 1


async def test_acquire_or_fail_raises_on_conflict_without_owning() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    holder = _guard(coord, sleeper, instance_id="holder")
    await holder.acquire_or_fail()
    contender = _guard(coord, sleeper, instance_id="contender")
    with pytest.raises(OwnershipAcquisitionError):
        await contender.acquire_or_fail()
    assert contender.state is OwnershipState.NOT_OWNER


# =========================================================================== #
# Renewal + ownership loss
# =========================================================================== #
async def test_renewal_renews_then_loses_on_failed_renewal() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    lost: list[bool] = []
    guard = _guard(coord, sleeper, on_ownership_lost=lambda: lost.append(True))
    await guard.acquire_or_fail()
    guard.start_renewal()
    await _settle()  # loop reaches the first sleeper await

    sleeper.grant()  # first renewal succeeds
    await _settle()
    assert coord.renew_calls == 1
    assert guard.state is OwnershipState.OWNER

    coord.renew_ok = False  # the lease is now fenced out
    sleeper.grant()  # second renewal fails → ownership lost
    await _settle()
    assert guard.state is OwnershipState.OWNERSHIP_LOST
    assert coord.mark_lost_calls == 1
    assert lost == [True]  # fail-closed notification fired exactly once
    assert guard._renewal_task is None or guard._renewal_task.done()


async def test_validate_enters_lost_when_fenced_out() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    tripped: list[bool] = []
    guard = _guard(coord, sleeper, on_ownership_lost=lambda: tripped.append(True))
    await guard.acquire_or_fail()
    coord.evict()  # a newer owner fenced this lease out
    assert await guard.validate() is False
    assert guard.state is OwnershipState.OWNERSHIP_LOST
    assert tripped == [True]


async def test_wait_lost_unblocks_on_loss() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    guard = _guard(coord, sleeper)
    await guard.acquire_or_fail()
    waiter = asyncio.create_task(guard.wait_lost())
    await _settle()
    assert not waiter.done()
    coord.evict()
    await guard.validate()
    await asyncio.wait_for(waiter, timeout=1.0)  # returns → loss observed


# =========================================================================== #
# Release discipline
# =========================================================================== #
async def test_release_only_when_still_owner() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    guard = _guard(coord, sleeper)
    await guard.acquire_or_fail()
    assert await guard.release() is True
    assert guard.state is OwnershipState.NOT_OWNER
    assert coord.release_calls == 1


async def test_lost_lease_is_never_released() -> None:
    coord, sleeper = _FakeCoordinator(), _PermitSleeper()
    guard = _guard(coord, sleeper)
    await guard.acquire_or_fail()
    coord.evict()
    await guard.validate()  # → OWNERSHIP_LOST
    assert await guard.release() is False
    assert coord.release_calls == 0  # never issues a fenced DEL for a lease it no longer owns
