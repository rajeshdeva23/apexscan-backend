"""Cross-process single-Dhan-owner interlock — Redis lease + fencing (PHASE H9A, ADR-030).

`SINGLE_DHAN_OWNER = TRUE` (ADR-027): the backend and market-ingestion must never both own the real
Dhan session. In-process config checks cannot see a two-container topology, so ownership is gated on
an exclusive, TTL-bounded **Redis lease** carrying a monotonic **fencing generation** (ADR-030,
Option I2). A process must hold a valid lease before any Dhan connect; a second contender fails
closed; a stale (paused/expired) holder is rejected on renew/release/validate by fencing.

This module is broker-neutral: it never imports the TickEngine, a strategy, an API route, or a Dhan
adapter, and it performs no Redis/task/IO at import. It is OFF by default — nothing composes it into
the production runtime. H9A implements and proves it **offline** (isolated Redis + fake providers);
it activates nothing and transfers no ownership (that is the separately-governed H9B).

Correctness rests on Redis atomicity (single-call Lua) and Redis-server time (lease TTL + acquire
timestamp via ``TIME``), never on drifting client clocks, and it **fails closed**: any Redis error
means ownership cannot be proven, which is no permission to own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from typing import Self

    from redis.commands.core import AsyncScript


class OwnerRole(StrEnum):
    """The two logical Dhan-owner roles (never both live at once)."""

    BACKEND = "backend"
    INGESTION = "ingestion"


class OwnershipState(StrEnum):
    """Bounded ownership lifecycle for a runtime (Redis is the authority; this is a local view)."""

    NOT_OWNER = "not_owner"
    OWNER = "owner"
    OWNERSHIP_LOST = (
        "ownership_lost"  # held a lease, then lost it (renew/validate failed / expired)
    )


class OwnershipLeaseConfig(BaseModel):
    """Frozen, validated lease timing + key contract (centralized; ADR-030)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    owner_key: str = Field(default="md:provider:ownership", min_length=1, max_length=128)
    fence_key: str = Field(default="md:provider:ownership:fence", min_length=1, max_length=128)
    lease_ttl_seconds: int = Field(default=30, ge=1, le=3_600)
    renewal_interval_seconds: int = Field(default=10, ge=1, le=3_600)

    @model_validator(mode="after")
    def _renewal_below_ttl(self) -> Self:
        """Fail closed unless ``0 < renewal_interval < lease_ttl`` (renew must beat expiry)."""
        if not 0 < self.renewal_interval_seconds < self.lease_ttl_seconds:
            raise ValueError(
                "ownership lease invariant violated: renewal_interval_seconds "
                f"({self.renewal_interval_seconds}) must be > 0 and < lease_ttl_seconds "
                f"({self.lease_ttl_seconds}) so a live owner renews before the lease expires."
            )
        return self


@dataclass(frozen=True, slots=True)
class OwnershipLease:
    """Proof-of-ownership token: the record a holder must match to renew/release/validate."""

    owner_role: OwnerRole
    instance_id: str
    fencing_generation: int


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    """The current owner as stored in Redis (a no-owner state is absence, not this type)."""

    owner_role: str
    instance_id: str
    fencing_generation: int
    acquired_at_ms: int


@dataclass(frozen=True, slots=True)
class OwnershipSnapshot:
    """Bounded, credential-free ownership diagnostics (never a token/PIN/secret)."""

    has_owner: bool
    owner_role: str | None
    instance_id: str | None
    fencing_generation: int | None
    acquire_success_total: int
    acquire_conflict_total: int
    renew_success_total: int
    renew_failure_total: int
    release_total: int
    ownership_lost_total: int
    redis_error_total: int


# Idempotent-or-conflict acquire: no live owner -> INCR fence, write record (TTL), return {1, gen};
# same (role, instance) already owns -> refresh TTL, keep generation, return {1, gen}; a *different*
# live owner holds it -> return {0, 0} (fail closed, never steal a valid lease). acquired_at_ms
# comes from the Redis server clock (TIME), never a client clock.
_ACQUIRE_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur then
  local o = cjson.decode(cur)
  if o.owner_role == ARGV[1] and o.instance_id == ARGV[2] then
    redis.call('SET', KEYS[1], cur, 'EX', tonumber(ARGV[3]))
    return {1, o.fencing_generation}
  end
  return {0, 0}
end
local gen = redis.call('INCR', KEYS[2])
local t = redis.call('TIME')
local now_ms = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
local rec = cjson.encode({
  owner_role = ARGV[1], instance_id = ARGV[2], fencing_generation = gen, acquired_at_ms = now_ms
})
redis.call('SET', KEYS[1], rec, 'EX', tonumber(ARGV[3]))
return {1, gen}
"""

# Refresh the TTL only if the current record matches (role, instance, generation) exactly.
_RENEW_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local o = cjson.decode(cur)
local match = o.owner_role == ARGV[1] and o.instance_id == ARGV[2]
if match and o.fencing_generation == tonumber(ARGV[3]) then
  redis.call('SET', KEYS[1], cur, 'EX', tonumber(ARGV[4]))
  return 1
end
return 0
"""

# Delete the record only if it matches (role, instance, generation) — never an unconditional DEL.
_RELEASE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local o = cjson.decode(cur)
local match = o.owner_role == ARGV[1] and o.instance_id == ARGV[2]
if match and o.fencing_generation == tonumber(ARGV[3]) then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


class RedisOwnershipCoordinator:
    """Atomic Redis lease + fencing coordinator; every operation fails closed on a Redis error."""

    def __init__(self, redis: Redis, config: OwnershipLeaseConfig) -> None:
        self._redis = redis
        self._config = config
        self._acquire: AsyncScript = redis.register_script(_ACQUIRE_LUA)
        self._renew: AsyncScript = redis.register_script(_RENEW_LUA)
        self._release: AsyncScript = redis.register_script(_RELEASE_LUA)
        self._acquire_success = 0
        self._acquire_conflict = 0
        self._renew_success = 0
        self._renew_failure = 0
        self._release_total = 0
        self._ownership_lost = 0
        self._redis_error = 0

    async def acquire(self, owner_role: OwnerRole, instance_id: str) -> OwnershipLease | None:
        """Acquire the exclusive lease (fresh or idempotent); ``None`` if another owner holds it.

        Fails closed to ``None`` on any Redis error — inability to prove ownership is no permission
        to own. A fresh acquisition mints a strictly higher fencing generation.
        """
        try:
            result = await self._acquire(
                keys=[self._config.owner_key, self._config.fence_key],
                args=[owner_role.value, instance_id, self._config.lease_ttl_seconds],
            )
        except RedisError:
            self._redis_error += 1
            return None
        acquired, generation = int(result[0]), int(result[1])
        if acquired != 1:
            self._acquire_conflict += 1
            return None
        self._acquire_success += 1
        return OwnershipLease(owner_role, instance_id, generation)

    async def renew(self, lease: OwnershipLease) -> bool:
        """Refresh the lease TTL iff it is still the authoritative owner; else fail-closed."""
        try:
            ok = await self._renew(
                keys=[self._config.owner_key],
                args=[
                    lease.owner_role.value,
                    lease.instance_id,
                    lease.fencing_generation,
                    self._config.lease_ttl_seconds,
                ],
            )
        except RedisError:
            self._redis_error += 1
            self._renew_failure += 1
            return False
        if int(ok) == 1:
            self._renew_success += 1
            return True
        self._renew_failure += 1
        return False

    async def release(self, lease: OwnershipLease) -> bool:
        """Delete the record iff this exact lease still owns it; never a newer owner's lease."""
        try:
            ok = await self._release(
                keys=[self._config.owner_key],
                args=[lease.owner_role.value, lease.instance_id, lease.fencing_generation],
            )
        except RedisError:
            self._redis_error += 1
            return False
        self._release_total += 1
        return int(ok) == 1

    async def validate(self, lease: OwnershipLease) -> bool:
        """Whether this exact lease is still the current owner; fails closed on a Redis error."""
        record = await self._read()
        if record is None:
            return False
        return (
            record.owner_role == lease.owner_role.value
            and record.instance_id == lease.instance_id
            and record.fencing_generation == lease.fencing_generation
        )

    async def mark_ownership_lost(self) -> None:
        """Count a detected ownership loss (renew/validate failed / lease expired)."""
        self._ownership_lost += 1

    async def snapshot(self) -> OwnershipSnapshot:
        """Bounded ownership diagnostics; the current-owner read fails closed to 'no owner'."""
        record = await self._read()
        return OwnershipSnapshot(
            has_owner=record is not None,
            owner_role=record.owner_role if record else None,
            instance_id=record.instance_id if record else None,
            fencing_generation=record.fencing_generation if record else None,
            acquire_success_total=self._acquire_success,
            acquire_conflict_total=self._acquire_conflict,
            renew_success_total=self._renew_success,
            renew_failure_total=self._renew_failure,
            release_total=self._release_total,
            ownership_lost_total=self._ownership_lost,
            redis_error_total=self._redis_error,
        )

    async def _read(self) -> OwnershipRecord | None:
        """Read the current owner; fail closed to ``None`` on any Redis *or* record-decode error.

        A corrupt/tampered record is treated exactly like an unreadable one — it can never be
        decoded into a valid owner, so it grants no permission to own and is counted as an error.
        """
        try:
            raw = await self._redis.get(self._config.owner_key)
            if raw is None:
                return None
            data = json.loads(raw)
            return OwnershipRecord(
                owner_role=str(data["owner_role"]),
                instance_id=str(data["instance_id"]),
                fencing_generation=int(data["fencing_generation"]),
                acquired_at_ms=int(data["acquired_at_ms"]),
            )
        except (RedisError, ValueError, KeyError, TypeError):
            self._redis_error += 1
            return None
