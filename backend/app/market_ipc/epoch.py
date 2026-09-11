"""Producer-epoch allocation for IPC dedup identity (DECOUPLING PHASE B; hardened in M1).

Dedup identity is ``(producer_id, producer_epoch, producer_sequence)``. ``producer_sequence``
resets to zero on every producer restart, so ``producer_epoch`` MUST change on each restart and
must never be reused for a given ``producer_id`` — otherwise a post-restart ``seq=1`` collides
with the previous run's ``seq=1``.

M1 makes epoch allocation durable against Redis state loss. The epoch authority is a
**producer-local, crash-safe file**, not Redis: a Redis ``FLUSHDB`` / volume loss / fresh
instance cannot reset the counter, so it can never hand out an already-used epoch merely because
Redis started empty (ADR-020). Each :meth:`allocate` advances a monotonic counter and persists
the new value with crash-safe atomicity **before** returning it, so a crash can only skip an
epoch (a harmless gap), never reuse one. Concurrent starts sharing a ``producer_id`` are
serialized by an exclusive file lock and each receive a distinct epoch. Missing state means a
first-ever start (epoch 0 → first allocation 1); corrupt/unreadable state fails closed (raises)
rather than silently resetting to a reusable low epoch.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

# Historical Redis key prefix (pre-M1). Retained only as a documented constant so operators
# recognise the legacy ``md:producer:epoch:*`` keys; the epoch authority is no longer Redis.
LEGACY_REDIS_EPOCH_KEY_PREFIX = "md:producer:epoch"

_STATE_SCHEMA = "apexscan-producer-epoch/1"
_SAFE_PRODUCER_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class EpochStateError(RuntimeError):
    """Durable producer-epoch state is unreadable/corrupt; allocation fails closed."""


@runtime_checkable
class EpochAllocator(Protocol):
    """Allocates a restart-unique, monotonic epoch for a logical producer."""

    async def allocate(self, producer_id: str) -> int:
        """Return a new epoch for ``producer_id`` that was never returned before."""
        ...


class DurableEpochAllocator:
    """Crash-safe, file-backed, lock-guarded monotonic epoch allocator (M1).

    The counter lives in ``<state_dir>/producer-epoch-<producer_id>.json`` on the producer's
    own durable volume, independent of Redis. It is Redis-loss-proof; its own failure model is
    the local volume (see ADR-020): the file survives process/container/host restart with the
    volume intact, and its loss is an explicit recovery case, not a silent epoch reuse.
    """

    def __init__(self, state_dir: Path) -> None:
        """Wire the allocator to the durable state directory (created on first use)."""
        self._state_dir = Path(state_dir)

    async def allocate(self, producer_id: str) -> int:
        """Advance and persist this producer's epoch, then return it (never reuses).

        Raises:
            ValueError: If ``producer_id`` is empty or not a safe filename component.
            EpochStateError: If existing durable state is corrupt/unreadable (fail closed).
            OSError: If the durable state cannot be read/written (fail closed).
        """
        self._validate(producer_id)
        # File + lock syscalls are blocking; this is a once-per-startup call, so offloading it
        # keeps the async contract without ever touching the per-event hot path.
        return await asyncio.to_thread(self._allocate_sync, producer_id)

    def _allocate_sync(self, producer_id: str) -> int:
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._state_dir / f"producer-epoch-{producer_id}.json"
        lock_path = path.with_suffix(".lock")
        # An exclusive lock serialises accidental concurrent starts sharing a producer_id, so
        # each read-increment-write is atomic across processes and every start gets a distinct
        # epoch (never colliding identities).
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = self._read_epoch(path, producer_id)
            nxt = current + 1
            self._atomic_write(path, producer_id, nxt)  # persist BEFORE returning
            return nxt
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @staticmethod
    def _validate(producer_id: str) -> None:
        if not producer_id:
            raise ValueError("producer_id must be non-empty")
        if not _SAFE_PRODUCER_ID.match(producer_id):
            raise ValueError("producer_id must match [A-Za-z0-9._-]{1,128} for durable epoch state")

    @staticmethod
    def _read_epoch(path: Path, producer_id: str) -> int:
        """Return the last persisted epoch, 0 if never written; raise on corruption."""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0  # first-ever start for this producer_id
        try:
            state = json.loads(raw)
            if (
                not isinstance(state, dict)
                or state.get("schema") != _STATE_SCHEMA
                or state.get("producer_id") != producer_id
            ):
                raise EpochStateError(f"unrecognised producer-epoch state at {path}")
            epoch = state["epoch"]
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
                raise EpochStateError(f"invalid epoch value in {path}")
        except (ValueError, KeyError) as error:
            # Never silently reset to 0 (a reusable low epoch) on a partial/garbled write.
            raise EpochStateError(f"corrupt producer-epoch state at {path}") from error
        return epoch

    def _atomic_write(self, path: Path, producer_id: str, epoch: int) -> None:
        """Durably persist ``epoch`` via temp-file + fsync + atomic replace + dir fsync."""
        payload = json.dumps({"schema": _STATE_SCHEMA, "producer_id": producer_id, "epoch": epoch})
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)  # atomic on POSIX
        dir_fd = os.open(self._state_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)  # persist the rename so the new epoch survives a crash
        finally:
            os.close(dir_fd)
