"""Pure current-session statistics logic for the Market Engine (P4.6B/E2; ADR-008/009).

One reconciliation primitive, :func:`apply_session_ohlc`, computes the next
:class:`SessionStatistics` from a provider session-OHLC aggregate and the instant it
became known — shared by every canonical source: the tick-carried aggregate (ADR-008,
:func:`update_session_statistics`) and the staged observation (ADR-009,
:func:`resolve_session_statistics`). It **never** fabricates a Tick. Everything here is
a pure classifier: no event bus, no MarketContext, no provider/HTTP/database/Redis
access, and no wall-clock read (``as_of`` is supplied), so identical inputs always
produce identical output.

Authority is **not** implied by the mere presence of an aggregate, and it is decided
**per canonical source class** (ADR-009 D6/D7): the primitive is told whether *this*
source is verified via a single ``source_verified`` bool, and the caller selects the
matching capability from :class:`SessionStatisticsAuthority` — ``staged_observation_verified``
for a staged observation, ``tick_aggregate_verified`` for a tick-carried aggregate. A
source that is not verified can never promote its aggregate to ``AUTHORITATIVE`` and,
critically, can never mutate an existing ``AUTHORITATIVE`` snapshot established by another
(verified) source — the prior snapshot is retained whole, so an unverified source cannot
advance an extremum, correct the open, refresh ``as_of``, or downgrade quality. Statistics
are established/advanced only during the regular ``LIVE_SESSION``; every other phase retains
the prior same-session state and never fabricates progression. A new trading date resets
(no previous-day leakage). Updates use one coherent snapshot whole — fields are never
merged, clamped, or reconstructed — and suspicious within-session changes (a stale
``as_of``, an open correction, or a regressing extremum) fail closed by retaining the
prior snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.market_engine.context import (
    MarketState,
    SessionContext,
    SessionStatistics,
    SessionStatisticsQuality,
)
from app.schemas.market_data import ProviderSessionOhlc, SessionStatisticsObservation, Tick


@dataclass(frozen=True, slots=True)
class SessionStatisticsAuthority:
    """Injected, immutable per-source capability gating authoritative statistics (ADR-009 D6/D7).

    Broker-neutral and deterministic. Authority is scoped to the canonical *source class*
    an aggregate entered the engine through, never to a provider, exchange, endpoint, or
    strategy. Each source is verified independently (P4.6D/E6): enabling one source can
    never authorize another. Both default to disabled — until its own source is verified, a
    valid aggregate from that source must never become ``AUTHORITATIVE``.

    Attributes:
        staged_observation_verified: Whether the staged-observation source
            (:class:`SessionStatisticsObservation`, ADR-009) is verified for authoritative use.
        tick_aggregate_verified: Whether the tick-carried aggregate source
            (:attr:`Tick.session_ohlc`, ADR-008) is verified for authoritative use.
    """

    staged_observation_verified: bool = False
    tick_aggregate_verified: bool = False


def apply_session_ohlc(
    *,
    aggregate: ProviderSessionOhlc | None,
    aggregate_as_of: datetime,
    session: SessionContext,
    previous: SessionStatistics | None,
    source_verified: bool,
) -> SessionStatistics | None:
    """Compute the next session statistics from one canonical aggregate (pure; source-neutral).

    The single reconciliation primitive shared by the tick-carried aggregate (ADR-008)
    and the staged observation (ADR-009). It never fabricates a Tick: it operates on
    the aggregate and the instant it became known. Authority is decided by the caller and
    passed in as ``source_verified`` — this primitive never inspects a provider, endpoint,
    or source class; an unverified source both fails to establish and cannot mutate an
    existing authoritative snapshot (the prior is retained whole).

    Args:
        aggregate: The canonical session OHLC, or ``None`` when the datum carries none.
        aggregate_as_of: When the aggregate became known (tz-aware UTC).
        session: The session facts for the accepted datum (P4.3).
        previous: The instrument's prior session statistics, or ``None``.
        source_verified: Whether the source this aggregate entered through is verified
            (ADR-008 D3/D4; ADR-009 D6). The caller selects the matching per-source bit.

    Returns:
        The next :class:`SessionStatistics` (``AUTHORITATIVE``), the prior value retained,
        or ``None`` when no authoritative statistics exist.
    """
    prior = _same_session_prior(previous, session)
    if session.market_state is not MarketState.LIVE_SESSION:
        return prior  # only the regular session establishes/advances statistics
    if not source_verified or aggregate is None:
        return prior  # unverified source or no aggregate: never fabricate/mutate, retain prior
    candidate = SessionStatistics(
        trading_date=session.trading_date,
        open_price=aggregate.open_price,
        high_price=aggregate.high_price,
        low_price=aggregate.low_price,
        quality=SessionStatisticsQuality.AUTHORITATIVE,
        as_of=aggregate_as_of,
    )
    if prior is None:
        return candidate  # first verified snapshot of the session
    return _reconciled(candidate, prior, aggregate_as_of)


def update_session_statistics(
    *,
    tick: Tick,
    session: SessionContext,
    previous: SessionStatistics | None,
    authority: SessionStatisticsAuthority,
) -> SessionStatistics | None:
    """Tick-path wrapper over :func:`apply_session_ohlc` (ADR-008 D1 transport).

    Gated solely by :attr:`SessionStatisticsAuthority.tick_aggregate_verified`; the
    staged-observation capability never authorizes the tick-carried aggregate.
    """
    return apply_session_ohlc(
        aggregate=tick.session_ohlc,
        aggregate_as_of=tick.event_timestamp,
        session=session,
        previous=previous,
        source_verified=authority.tick_aggregate_verified,
    )


def resolve_session_statistics(
    *,
    aggregate: ProviderSessionOhlc | None,
    aggregate_as_of: datetime,
    staged: SessionStatisticsObservation | None,
    session: SessionContext | None,
    previous: SessionStatistics | None,
    authority: SessionStatisticsAuthority,
) -> tuple[SessionStatistics | None, SessionStatisticsObservation | None]:
    """Resolve one accepted datum's statistics and the remaining staged observation (pure).

    An eligible staged observation (the authoritative-candidate source, ADR-009 D7) supplies
    the aggregate and is consumed, taking precedence over the datum's provisional tick-carried
    aggregate; a stale prior-day observation is dropped (no leakage, ADR-009 D9); otherwise the
    observation is retained pending (future date, or same date before ``LIVE_SESSION``) and the
    tick-carried aggregate is applied. Precedence chooses *which* source is examined; the
    per-source authority bit chooses whether it may be trusted — an examined-first but
    unverified source can never mutate authoritative state (ADR-009 D7). No version, event.

    Returns:
        ``(next statistics, next staged observation)``.
    """
    if session is None:
        return previous, staged  # no session classification: cannot apply; keep pending
    next_staged = staged
    if staged is not None:
        if staged.trading_date < session.trading_date:
            next_staged = None  # stale prior-day observation dropped — no leakage
        elif (
            staged.trading_date == session.trading_date
            and session.market_state is MarketState.LIVE_SESSION
        ):
            applied = apply_session_ohlc(
                aggregate=staged.session_ohlc,
                aggregate_as_of=staged.observed_at,
                session=session,
                previous=previous,
                source_verified=authority.staged_observation_verified,
            )
            return applied, None  # observation applied and consumed (precedence over the tick)
    applied = apply_session_ohlc(
        aggregate=aggregate,
        aggregate_as_of=aggregate_as_of,
        session=session,
        previous=previous,
        source_verified=authority.tick_aggregate_verified,
    )
    return applied, next_staged


def _same_session_prior(
    previous: SessionStatistics | None, session: SessionContext
) -> SessionStatistics | None:
    """Return the prior statistics only if they belong to the current trading date."""
    if previous is None or previous.trading_date != session.trading_date:
        return None  # a new trading date starts clean — no previous-day leakage
    return previous


def _reconciled(
    candidate: SessionStatistics, prior: SessionStatistics, aggregate_as_of: datetime
) -> SessionStatistics:
    """Accept a coherent forward progression, else retain the prior snapshot (fail closed).

    Within a session the prior is retained unchanged when the aggregate is stale, the
    open changed (an unverified correction), or an extremum regressed; an identical
    aggregate reuses the prior to avoid churn. Otherwise the whole new snapshot is
    accepted — never a field-merged hybrid (ADR-008/009).
    """
    if aggregate_as_of < prior.as_of:
        return prior  # stale aggregate never regresses fresher state
    new_open, new_high, new_low = candidate.open_price, candidate.high_price, candidate.low_price
    old_open, old_high, old_low = prior.open_price, prior.high_price, prior.low_price
    if (
        new_open is None
        or new_high is None
        or new_low is None
        or old_open is None
        or old_high is None
        or old_low is None
    ):
        return prior  # authoritative snapshots always carry prices; fail closed otherwise
    if new_open != old_open:
        return prior  # open correction is unverified — fail closed (§20)
    if new_high < old_high or new_low > old_low:
        return prior  # a running extremum must not move the wrong way (§18/§19)
    if new_high == old_high and new_low == old_low:
        return prior  # identical snapshot — reuse, no churn (§15/§21)
    return candidate
