"""Pure snapshot projections for ingestion and backend + IPC version seam (PHASE E).

Both ingestion (which instruments to subscribe) and backend (which instruments are expected +
their sectors) are consumers of the SAME promoted :class:`UniverseSnapshot`, so they derive the
same identities and the same ``universe_version`` by construction. These are pure transforms — no
WebSocket, no Dhan call, no subscription mutation, no strategy/scanner/sector execution.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict

from app.market_engine.session import TradingCalendar
from app.market_universe.snapshot import CANDIDATE_VERSION, UniverseSnapshot
from app.market_universe.store import UniverseSnapshotStore

_MAX_ROLLOVER_LOOKAHEAD_DAYS = 14  # bounded search for the next trading day

TradingDateSource = Callable[[], date]


class SubscriptionEntry(BaseModel):
    """One ingestion subscription target: identity + provider mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: str
    provider_security_id: str
    exchange_segment: str


class SubscriptionUniverse(BaseModel):
    """The ingestion-side view of a snapshot (what to subscribe)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    universe_version: int
    trading_date: date
    entries: tuple[SubscriptionEntry, ...]


class BackendExpectedUniverse(BaseModel):
    """The backend-side view of a snapshot (expected identities + sector membership)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    universe_version: int
    trading_date: date
    identities: tuple[str, ...]
    sector_membership: dict[str, str | None]


def to_subscription_universe(snapshot: UniverseSnapshot) -> SubscriptionUniverse:
    """Project a snapshot to the ingestion subscription view (pure)."""
    return SubscriptionUniverse(
        universe_version=snapshot.universe_version,
        trading_date=snapshot.trading_date,
        entries=tuple(
            SubscriptionEntry(
                identity=item.identity,
                provider_security_id=item.provider_security_id,
                exchange_segment=item.exchange_segment,
            )
            for item in snapshot.instruments
        ),
    )


def to_backend_universe(snapshot: UniverseSnapshot) -> BackendExpectedUniverse:
    """Project a snapshot to the backend expected-universe view (pure)."""
    return BackendExpectedUniverse(
        universe_version=snapshot.universe_version,
        trading_date=snapshot.trading_date,
        identities=snapshot.identities,
        sector_membership=snapshot.sector_membership,
    )


def next_trading_date(calendar: TradingCalendar, after: date) -> date:
    """Return the first trading day strictly after ``after`` (weekend/holiday aware).

    Reuses :meth:`TradingCalendar.is_trading_day`; never uses a naive ``date + 1 day``. Bounded
    lookahead so a misconfigured calendar cannot loop forever.
    """
    candidate = after
    for _ in range(_MAX_ROLLOVER_LOOKAHEAD_DAYS):
        candidate = candidate + timedelta(days=1)
        if calendar.is_trading_day(candidate):
            return candidate
    raise ValueError(f"no trading day within {_MAX_ROLLOVER_LOOKAHEAD_DAYS} days after {after}")


class SnapshotUniverseVersion:
    """IPC universe-version authority backed by the active snapshot (capability only, not wired).

    Implements the publisher's ``UniverseVersionSource`` seam so Phase H/I can eventually replace
    the provisional ``StaticUniverseVersion``. Returns ``CANDIDATE_VERSION`` (0) when no snapshot
    is effective yet, so a not-yet-promoted universe never masquerades as a real version.
    """

    def __init__(
        self, *, store: UniverseSnapshotStore, trading_date_source: TradingDateSource
    ) -> None:
        self._store = store
        self._trading_date_source = trading_date_source

    def current_universe_version(self) -> int:
        """Return the effective snapshot's version for the current trading date, else 0."""
        snapshot = self._store.active_for(self._trading_date_source())
        return snapshot.universe_version if snapshot is not None else CANDIDATE_VERSION
