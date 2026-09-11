"""DECOUPLING-M1: durable, collision-safe producer-epoch allocation.

Proves the M1 invariant: for one ``producer_id`` no two allocations ever return the same epoch
under process/host restart, Redis state loss (Redis is not involved), concurrent starts, or a
crash between allocations — and that corrupt/partial durable state fails closed rather than
silently resetting to a reusable low epoch.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.market_ipc import DurableEpochAllocator, EpochStateError, MarketIpcConfig

_PRODUCER = "market-ingestion"


def _state_file(state_dir: Path, producer_id: str = _PRODUCER) -> Path:
    return state_dir / f"producer-epoch-{producer_id}.json"


# --- A/B/O: fresh start, monotonic across incarnations, restart loop ---------- #
async def test_fresh_first_start_allocates_one(tmp_path: Path) -> None:
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 1


async def test_monotonic_across_incarnations(tmp_path: Path) -> None:
    # Each allocate() models one producer start (a new incarnation).
    epochs = [await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) for _ in range(5)]
    assert epochs == [1, 2, 3, 4, 5]


async def test_restart_loop_never_reuses(tmp_path: Path) -> None:
    seen: set[int] = set()
    for _ in range(50):  # a tight crash/restart loop — always a fresh incarnation
        epoch = await DurableEpochAllocator(tmp_path).allocate(_PRODUCER)
        assert epoch not in seen
        seen.add(epoch)
    assert seen == set(range(1, 51))


# --- C/D: clean restart & crash between allocations -------------------------- #
async def test_clean_restart_continues_from_persisted(tmp_path: Path) -> None:
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 1
    # A brand-new allocator instance = a restarted process reading the same durable dir.
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 2


async def test_persist_before_return_so_crash_only_skips(tmp_path: Path) -> None:
    # The value is persisted BEFORE it is returned, so a crash after persist (returned epoch
    # discarded) skips an epoch on the next start rather than reusing it.
    first = await DurableEpochAllocator(tmp_path).allocate(_PRODUCER)  # persists 1
    on_disk = json.loads(_state_file(tmp_path).read_text())["epoch"]
    assert on_disk == first == 1
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 2  # never reuses 1


# --- F/G: Redis-loss proof (Redis is not the authority) --------------------- #
async def test_epoch_is_redis_independent(tmp_path: Path) -> None:
    # The allocator takes only a filesystem dir — no Redis handle — so a Redis FLUSHDB / volume
    # loss / fresh instance cannot reset it. State survives entirely in the local file.
    alloc = DurableEpochAllocator(tmp_path)
    await alloc.allocate(_PRODUCER)
    await alloc.allocate(_PRODUCER)
    # "Redis loss" is simulated by the fact that nothing here depends on Redis: a new incarnation
    # still advances from the durable file, never colliding with an already-emitted epoch.
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 3


# --- H/I: corruption / partial write fails closed --------------------------- #
@pytest.mark.parametrize(
    "corrupt",
    [
        "",  # empty (partial write)
        "{",  # truncated JSON
        "not json",
        json.dumps({"schema": "wrong/1", "producer_id": _PRODUCER, "epoch": 5}),
        json.dumps({"schema": "apexscan-producer-epoch/1", "producer_id": "other", "epoch": 5}),
        json.dumps({"schema": "apexscan-producer-epoch/1", "producer_id": _PRODUCER}),  # no epoch
        json.dumps({"schema": "apexscan-producer-epoch/1", "producer_id": _PRODUCER, "epoch": -1}),
        json.dumps({"schema": "apexscan-producer-epoch/1", "producer_id": _PRODUCER, "epoch": "5"}),
        json.dumps(
            {"schema": "apexscan-producer-epoch/1", "producer_id": _PRODUCER, "epoch": True}
        ),
    ],
)
async def test_corrupt_state_fails_closed(tmp_path: Path, corrupt: str) -> None:
    _state_file(tmp_path).write_text(corrupt, encoding="utf-8")
    with pytest.raises(EpochStateError):
        await DurableEpochAllocator(tmp_path).allocate(_PRODUCER)
    # Never silently reset to a reusable low epoch.


# --- J: concurrent starts get distinct epochs ------------------------------- #
async def test_concurrent_starts_are_unique(tmp_path: Path) -> None:
    # 20 concurrent allocations sharing a producer_id/dir — the exclusive file lock serialises
    # them so every start gets a distinct epoch (never a colliding identity).
    epochs = await asyncio.gather(
        *(DurableEpochAllocator(tmp_path).allocate("p") for _ in range(20))
    )
    assert sorted(epochs) == list(range(1, 21))


# --- validation / path-traversal (security) --------------------------------- #
@pytest.mark.parametrize("bad", ["", "../evil", "a/b", "with space", "x" * 129, "sneaky/../x"])
async def test_unsafe_producer_id_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="producer_id"):
        await DurableEpochAllocator(tmp_path).allocate(bad)
    # Rejected before any filesystem access: nothing escaped (or was created in) the state dir.
    assert not any("evil" in name for name in os.listdir(tmp_path))


async def test_distinct_producer_ids_are_independent(tmp_path: Path) -> None:
    a = await DurableEpochAllocator(tmp_path).allocate("producer-a")
    b = await DurableEpochAllocator(tmp_path).allocate("producer-b")
    assert a == b == 1  # separate counters, no cross-contamination


# --- clock independence (M/N) ------------------------------------------------ #
async def test_no_wall_clock_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Freeze/rewind any wall clock: epoch allocation must be unaffected (counter, not time).
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: 0.0)
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 1
    monkeypatch.setattr(_time, "time", lambda: -10_000.0)  # clock moved backwards
    assert await DurableEpochAllocator(tmp_path).allocate(_PRODUCER) == 2


# --- R/S: does not enable IPC; no secrets in durable state ------------------- #
def test_m1_does_not_enable_ipc() -> None:
    assert MarketIpcConfig().enabled is False


async def test_durable_state_contains_no_secrets(tmp_path: Path) -> None:
    await DurableEpochAllocator(tmp_path).allocate(_PRODUCER)
    body = _state_file(tmp_path).read_text(encoding="utf-8").lower()
    for secret in ("token", "totp", "pin", "secret", "password", "authorization"):
        assert secret not in body
    assert set(json.loads(body)) == {"schema", "producer_id", "epoch"}


# --- §24 property/adversarial: no duplicate epoch under a restart storm ------ #
async def test_property_no_duplicate_epoch_under_restart_storm(tmp_path: Path) -> None:
    all_epochs: list[int] = []
    for _ in range(200):  # 200 modelled incarnations
        all_epochs.append(await DurableEpochAllocator(tmp_path).allocate(_PRODUCER))
    assert len(set(all_epochs)) == len(all_epochs)  # every epoch distinct
    assert all_epochs == sorted(all_epochs)  # strictly monotonic


# --- §25 failure injection: write failure fails closed ---------------------- #
async def test_write_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.market_ipc.epoch as epoch_mod

    def _boom(*_a: object, **_k: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(epoch_mod.os, "replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        await DurableEpochAllocator(tmp_path).allocate(_PRODUCER)
