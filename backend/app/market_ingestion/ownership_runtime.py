"""Ownership lifecycle orchestration around the H9A fenced-lease primitive (DECOUPLING H9B).

The H9A :class:`RedisOwnershipCoordinator` is a proven, stateless-per-call primitive (acquire /
renew / release / validate). It has no lifecycle: nothing generates a per-incarnation identity,
renews on a cadence, notices a lost lease, or releases on a clean stop. This module is exactly that
missing orchestration layer — one :class:`ProviderOwnershipGuard` per runtime **incarnation** that:

    acquire  -> validate  -> (permit token mint / provider connect)
             -> renew on a bounded cadence (injected clock/sleeper — deterministically testable)
             -> ownership loss (renew/validate fails) => mark lost + fail-closed notification
             -> release only on a clean stop, and only a lease this incarnation still owns

Hard invariants (ADR-030): a stale owner never renews, never releases another owner's lease, and a
lost lease is never released (releasing after loss could delete a newer owner's fencing state — the
release Lua is fenced, but the guard also refuses structurally). One incarnation = one
``instance_id`` (a fresh ``uuid4`` per guard); a provider reconnect inside the same incarnation
keeps the guard and therefore the same ``instance_id``, while a process/runtime restart builds one
and a new ``instance_id``. ``instance_id`` is kept distinct from ``producer_id``/``producer_epoch``/
``fencing_generation`` — it only answers "is this the same owner?".

Off by default: nothing constructs a guard unless ownership is explicitly enabled (H9B is offline).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.market_ingestion.ownership import OwnershipState

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.config import Settings
    from app.market_ingestion.ownership import (
        OwnerRole,
        OwnershipLease,
        RedisOwnershipCoordinator,
    )

logger = logging.getLogger(__name__)


def build_provider_ownership_guard(
    settings: Settings, role: OwnerRole, *, instance_id: str | None = None
) -> ProviderOwnershipGuard | None:
    """Build one from-settings ownership guard, or ``None`` when ownership is disabled (default).

    Both the legacy backend path and the decoupled ingestion service call this so they compete for
    the SAME authority domain: one Redis (``settings.redis_url``) and one ``OwnershipLeaseConfig``
    (identical owner/fence keys). The guard owns and closes the dedicated Redis client it creates.
    """
    if not settings.market_ownership_enabled:
        return None
    from redis.asyncio import Redis

    from app.market_ingestion.ownership import RedisOwnershipCoordinator

    redis: Redis = Redis.from_url(settings.redis_url)
    coordinator = RedisOwnershipCoordinator(redis, settings.market_ownership_config())
    return ProviderOwnershipGuard(
        coordinator=coordinator,
        role=role,
        renewal_interval_seconds=settings.market_ownership_renewal_interval_seconds,
        instance_id=instance_id,
        redis_to_close=redis,
    )


class OwnershipAcquisitionError(RuntimeError):
    """A runtime could not acquire/validate the exclusive provider-ownership lease (fail-closed).

    Raised before any Dhan token mint / provider connect, so a contender that loses the lease never
    reaches the provider. The message names the role, never a secret.
    """


class ProviderOwnershipGuard:
    """Per-incarnation ownership lifecycle around the fenced-lease coordinator (ADR-030, H9B)."""

    def __init__(
        self,
        *,
        coordinator: RedisOwnershipCoordinator,
        role: OwnerRole,
        renewal_interval_seconds: float,
        instance_id: str | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_ownership_lost: Callable[[], None] | None = None,
        redis_to_close: Redis | None = None,
    ) -> None:
        """Wire the guard for one incarnation; generate a fresh ``instance_id`` unless supplied.

        ``redis_to_close`` is a Redis client the guard OWNS and closes once on teardown (the
        from-settings composition creates a dedicated client for the coordinator); leave it ``None``
        when the coordinator's client is owned elsewhere (tests share one client).
        """
        self._coordinator = coordinator
        self._role = role
        self._instance_id = instance_id or uuid.uuid4().hex
        self._renewal_interval = renewal_interval_seconds
        self._sleep = sleep or asyncio.sleep
        self._on_ownership_lost = on_ownership_lost
        self._redis_to_close = redis_to_close
        self._redis_closed = False
        self._state = OwnershipState.NOT_OWNER
        self._lease: OwnershipLease | None = None
        self._renewal_task: asyncio.Task[None] | None = None
        self._lost_event = asyncio.Event()

    def set_on_ownership_lost(self, callback: Callable[[], None]) -> None:
        """Register the fail-closed notification fired on a detected ownership loss (light)."""
        self._on_ownership_lost = callback

    @property
    def instance_id(self) -> str:
        """The per-incarnation ownership identity (stable across provider reconnects)."""
        return self._instance_id

    @property
    def role(self) -> OwnerRole:
        """The owner role this guard competes for."""
        return self._role

    @property
    def state(self) -> OwnershipState:
        """Local ownership lifecycle view (Redis remains the authority)."""
        return self._state

    @property
    def fencing_generation(self) -> int | None:
        """The fencing generation of the currently-held lease, or ``None`` if not owning."""
        return self._lease.fencing_generation if self._lease is not None else None

    async def acquire_or_fail(self) -> OwnershipLease:
        """Acquire then validate the exclusive lease; raise fail-closed if either does not hold.

        This is the ordering invariant's front half — a caller MUST await this and only then permit
        a token mint / provider connect. A conflict (another live owner) or an immediate validation
        miss raises :class:`OwnershipAcquisitionError`; no partial ownership state is left behind.
        """
        lease = await self._coordinator.acquire(self._role, self._instance_id)
        if lease is None:
            raise OwnershipAcquisitionError(
                f"could not acquire the {self._role.value} provider-ownership lease "
                "(another live owner holds it, or Redis is unavailable — fail closed)"
            )
        self._lease = lease
        self._state = OwnershipState.OWNER
        if not await self._coordinator.validate(lease):
            self._state = OwnershipState.OWNERSHIP_LOST
            self._lease = None
            raise OwnershipAcquisitionError(
                f"the {self._role.value} ownership lease was not valid immediately after acquire "
                "(fenced out by a newer owner — fail closed)"
            )
        return lease

    async def validate(self) -> bool:
        """Whether this incarnation still owns the lease; on a miss, enter the lost path.

        Wired as the supervisor's pre-reconnect guard: a stale owner that lost the lease must not
        reconnect. Returns ``False`` (and fails closed) whenever ownership cannot be proven.
        """
        if self._state is not OwnershipState.OWNER or self._lease is None:
            return False
        if await self._coordinator.validate(self._lease):
            return True
        await self._enter_lost()
        return False

    def start_renewal(self) -> None:
        """Launch the bounded renewal loop (idempotent). Must already own the lease."""
        if self._renewal_task is not None:
            return
        if self._state is not OwnershipState.OWNER:
            raise OwnershipAcquisitionError("cannot start lease renewal before acquiring ownership")
        self._renewal_task = asyncio.create_task(self._renew_loop())

    async def _renew_loop(self) -> None:
        """Renew on the injected cadence; a failed renewal enters the lost path and ends it."""
        while self._state is OwnershipState.OWNER:
            await self._sleep(self._renewal_interval)
            if self._state is not OwnershipState.OWNER or self._lease is None:
                return
            if not await self._coordinator.renew(self._lease):
                await self._enter_lost()
                return

    async def _enter_lost(self) -> None:
        """Mark ownership lost (idempotent), count it, and fire the fail-closed notification.

        A lost lease is intentionally NOT released — it may already belong to a newer owner, and
        releasing it (even fenced) is the wrong signal. The notification is synchronous and must be
        lightweight (e.g. set a terminal event); the caller fails closed on its own task, so the
        notification never cancels the renewal loop from within itself.
        """
        if self._state is OwnershipState.OWNERSHIP_LOST:
            return
        self._state = OwnershipState.OWNERSHIP_LOST
        self._lease = None
        await self._coordinator.mark_ownership_lost()
        logger.warning("provider ownership lost for role %s; failing closed", self._role.value)
        self._lost_event.set()
        if self._on_ownership_lost is not None:
            self._on_ownership_lost()

    async def wait_lost(self) -> None:
        """Block until this incarnation detects an ownership loss (fail-closed owner signal)."""
        await self._lost_event.wait()

    async def release(self) -> bool:
        """Release the lease on a clean stop only; never a lost/absent lease (idempotent).

        Cancels the renewal loop first, then deletes the record iff this exact incarnation still
        owns it (the coordinator's release Lua is fenced). Returns whether a release happened.
        """
        await self._cancel_renewal()
        if self._state is not OwnershipState.OWNER or self._lease is None:
            await self._close_redis()  # never re-release a lost lease, but still free the client
            return False
        lease = self._lease
        self._lease = None
        self._state = OwnershipState.NOT_OWNER
        released = await self._coordinator.release(lease)
        await self._close_redis()
        return released

    async def _close_redis(self) -> None:
        """Close the guard-owned Redis client at most once (idempotent; close faults swallowed)."""
        if self._redis_to_close is None or self._redis_closed:
            return
        self._redis_closed = True
        with contextlib.suppress(Exception):
            await self._redis_to_close.aclose()

    async def _cancel_renewal(self) -> None:
        """Cancel and await the renewal loop, tolerating normal cancellation (idempotent)."""
        task = self._renewal_task
        self._renewal_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
