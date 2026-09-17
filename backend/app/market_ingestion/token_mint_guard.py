"""Cross-process Dhan token-mint throttle — persisted Redis metadata (PHASE H9C-P3, Gate H).

Dhan enforces an approximately two-minute cooldown between access-token generations, and answers a
too-soon request with a rate-limit body (:class:`ProviderRateLimitError`). The runtime token is held
in process memory only, so a backend recreate / an ownership handoff can re-mint too soon and
crash-loop the provider. This module bounds that at the runtime boundary with a **persisted,
cross-process** mint reservation, shared by the backend, the ingestion service, and any sanctioned
diagnostic mint path.

It is a SEPARATE concern from the ownership lease (ADR-030): the lease decides *who* may use Dhan;
this decides *when* an already-authorized owner may mint a new token. It stores only metadata (last
mint timestamp + the reserving owner's role/instance/fence) — never the access token — and it fails
closed: if the durable state cannot be atomically determined, no mint is permitted. The timestamp is
the Redis server clock (``TIME``), never a drifting client clock. Off by default: nothing constructs
it unless ownership is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.commands.core import AsyncScript

    from app.market_ingestion.ownership import OwnershipLease


class TokenMintConfig(BaseModel):
    """Frozen, validated token-mint throttle contract (metadata key + cooldown)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    mint_key: str = Field(default="md:provider:token:mint", min_length=1, max_length=128)
    cooldown_seconds: int = Field(default=120, ge=1, le=3_600)


@dataclass(frozen=True, slots=True)
class MintDecision:
    """The outcome of a reservation attempt: whether a mint is permitted, and the cooldown left."""

    allowed: bool
    remaining_seconds: float


# Atomic reserve-or-deny against the Redis server clock. If a prior mint is still inside the
# cooldown window -> {0, remaining_ms} (DENIED). Otherwise record this mint (metadata only, TTL =
# cooldown so the record self-expires when the window ends) and return {1, 0} (ALLOWED). The token
# itself is never stored.
_RESERVE_LUA = """
local t = redis.call('TIME')
local now_ms = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
local cooldown_ms = tonumber(ARGV[4])
local cur = redis.call('GET', KEYS[1])
if cur then
  local o = cjson.decode(cur)
  local elapsed = now_ms - tonumber(o.last_mint_at_ms)
  if elapsed < cooldown_ms then
    return {0, cooldown_ms - elapsed}
  end
end
local rec = cjson.encode({
  last_mint_at_ms = now_ms, owner_role = ARGV[1], instance_id = ARGV[2],
  fencing_generation = tonumber(ARGV[3])
})
redis.call('SET', KEYS[1], rec, 'EX', tonumber(ARGV[5]))
return {1, 0}
"""


class RedisTokenMintGuard:
    """Atomic, persisted mint throttle; fails closed (no mint) on any Redis or decode error."""

    def __init__(self, redis: Redis, config: TokenMintConfig) -> None:
        self._redis = redis
        self._config = config
        self._reserve: AsyncScript = redis.register_script(_RESERVE_LUA)

    async def reserve_mint(self, lease: OwnershipLease) -> MintDecision:
        """Atomically reserve a token mint for ``lease``'s owner, or deny within the cooldown.

        Records this mint's timestamp (Redis server clock) + the reserving owner's role/instance/
        fence when allowed. A prior mint still inside the cooldown denies with the window remaining.
        Any Redis error or malformed result fails closed to DENIED with the full cooldown left — an
        indeterminate durable state is never read as permission to mint.
        """
        ttl_seconds = max(1, self._config.cooldown_seconds)
        try:
            result = await self._reserve(
                keys=[self._config.mint_key],
                args=[
                    lease.owner_role.value,
                    lease.instance_id,
                    lease.fencing_generation,
                    self._config.cooldown_seconds * 1000,
                    ttl_seconds,
                ],
            )
            allowed = int(result[0]) == 1
            remaining_seconds = int(result[1]) / 1000.0
        except (RedisError, ValueError, TypeError, IndexError):
            return MintDecision(
                allowed=False, remaining_seconds=float(self._config.cooldown_seconds)
            )
        return MintDecision(allowed=allowed, remaining_seconds=remaining_seconds)


class TokenMintThrottledError(RuntimeError):
    """A token mint was refused because the cross-process cooldown had not elapsed (fail-closed).

    Raised before any Dhan token mint / provider connect, so a successor or a too-soon redeploy
    never reaches the provider inside the cooldown window. The message names the role and the
    remaining seconds, never a secret.
    """
