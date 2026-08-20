"""Composition service: refresh current-session statistics and stage them (P4.6E4; ADR-009).

Lives in the composition layer (outside ``app.market_engine`` and the provider adapters)
so the Market Engine stays sole writer and provider-blind. One :meth:`refresh` call
performs one logical batch: it captures a single governed observation instant from the
injected clock, asks a broker-neutral :class:`SessionStatisticsSource` for canonical
observations over the requested instruments, validates the source result at this
composition boundary, and stages each observation through
:meth:`InstrumentStateRegistry.stage_session_statistics_observation`.

It mints no MarketContext version, publishes no event, fabricates no Tick, establishes
no authority (ADR-009 D6 — production authority stays disabled until E6), decides no
consumer freshness (E5), and owns no recurring loop/scheduler (a caller drives cadence;
requirement-driven activation is E5). Staged observations surface on the next accepted
market datum via the Market Engine (P4.6E2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from app.adapters.base import ProviderContractViolationError, SessionStatisticsSource
from app.market_engine.clock import Clock, SystemClock
from app.market_engine.state import InstrumentStateRegistry
from app.schemas.market_data import Instrument, SessionStatisticsObservation


@dataclass(frozen=True, slots=True)
class SessionStatisticsRefreshOutcome:
    """A provider-neutral summary of one refresh (no provider data, no strategy meaning).

    Attributes:
        trading_date: The canonical exchange trading date the refresh targeted.
        observed_at: The single governed instant the refresh's snapshot was observed.
        requested_count: Distinct instruments requested.
        observation_count: Canonical observations the source returned (may be fewer than
            requested — missing/malformed instruments are withheld upstream).
        staged_count: Observations staged into per-instrument state (equals
            ``observation_count`` once the source result validates).
    """

    trading_date: date
    observed_at: datetime
    requested_count: int
    observation_count: int
    staged_count: int


class SessionStatisticsRefreshService:
    """Drives one session-statistics refresh from a source into per-instrument staging."""

    def __init__(
        self,
        *,
        source: SessionStatisticsSource,
        registry: InstrumentStateRegistry,
        clock: Clock | None = None,
    ) -> None:
        """Wire the service to a broker-neutral source, the state registry, and a clock.

        Args:
            source: The broker-neutral session-statistics source (ADR-009 D2).
            registry: The per-instrument state registry that owns staging.
            clock: Injected UTC clock; defaults to the system clock. Read once per
                refresh to stamp the observation instant (never per instrument).
        """
        self._source = source
        self._registry = registry
        self._clock: Clock = clock or SystemClock()

    async def refresh(
        self, instruments: Sequence[Instrument], *, trading_date: date
    ) -> SessionStatisticsRefreshOutcome:
        """Refresh and stage session statistics for the given instruments (one batch).

        Requested instruments are deduplicated and canonically ordered; an empty request
        performs no source call. A single ``observed_at`` is captured for the whole batch.
        The source result is fully validated (every observation was requested, unique, and
        for the requested trading date) **before** any staging, so a contract violation
        stages nothing. Observations are then staged in canonical order.

        Args:
            instruments: The instruments to refresh (duplicates collapse).
            trading_date: The caller-resolved canonical exchange trading date.

        Returns:
            A :class:`SessionStatisticsRefreshOutcome` with deterministic counts.

        Raises:
            ProviderContractViolationError: If the source returns an unrequested,
                duplicate, or wrong-trading-date observation (fail closed, nothing staged).
        """
        requested = _deduplicated_ordered(instruments)
        observed_at = self._clock.now()  # one governed instant for the whole batch
        if not requested:
            return SessionStatisticsRefreshOutcome(
                trading_date=trading_date,
                observed_at=observed_at,
                requested_count=0,
                observation_count=0,
                staged_count=0,
            )
        observations = await self._source.load_session_statistics(
            requested, trading_date=trading_date, observed_at=observed_at
        )
        _validate_source_result(observations, requested=requested, trading_date=trading_date)
        for observation in sorted(
            observations, key=lambda item: (item.instrument.exchange, item.instrument.symbol)
        ):
            self._registry.stage_session_statistics_observation(observation.instrument, observation)
        return SessionStatisticsRefreshOutcome(
            trading_date=trading_date,
            observed_at=observed_at,
            requested_count=len(requested),
            observation_count=len(observations),
            staged_count=len(observations),
        )


def _deduplicated_ordered(instruments: Sequence[Instrument]) -> tuple[Instrument, ...]:
    """Return the distinct instruments in deterministic canonical order."""
    unique = list(dict.fromkeys(instruments))
    return tuple(sorted(unique, key=lambda instrument: (instrument.exchange, instrument.symbol)))


def _validate_source_result(
    observations: Sequence[SessionStatisticsObservation],
    *,
    requested: Sequence[Instrument],
    trading_date: date,
) -> None:
    """Fail closed before staging if the source result breaks its contract (ADR-009 D2).

    ``observed_at`` coherence is deliberately not enforced: the generic port permits a
    future provider to stamp per-instrument snapshot times (ADR-009 D1). The current
    source path supplies one shared ``observed_at`` by construction.
    """
    requested_set = set(requested)
    seen: set[Instrument] = set()
    for observation in observations:
        if observation.instrument not in requested_set:
            raise ProviderContractViolationError()  # unrequested instrument
        if observation.instrument in seen:
            raise ProviderContractViolationError()  # duplicate observation
        seen.add(observation.instrument)
        if observation.trading_date != trading_date:
            raise ProviderContractViolationError()  # wrong trading date
