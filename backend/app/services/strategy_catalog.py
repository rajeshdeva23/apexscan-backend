"""Explicit, deterministic, provider-neutral production strategy catalog (ADR-013).

The catalog is the single source of the production-known concrete strategies. Each entry
co-locates a strategy with its default configuration and (when scanner-enabled) its
:class:`~app.services.cross_instrument_scanner.ScannerRankingPolicy`, validating fail-fast
that the three agree (REG2). Composition resolves only the explicitly enabled entries
(``Settings.strategies_enabled``); an enabled id absent from the catalog fails closed
(REG3/REG14). There is no import-time/auto-discovery registration — the catalog is
constructed explicitly (REG1).

It is broker-neutral: it references concrete strategy implementations and the scanner
policy contract only, never a provider adapter, credential, or transport type. Provider
composition *invokes* it; strategy definitions stay broker-independent (REG11; ADR-003).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.services.cross_instrument_scanner import ScannerOrdering, ScannerRankingPolicy
from app.strategies.configuration import StrategyConfiguration
from app.strategies.contracts import Strategy
from app.strategies.implementations.narrow_cpr import NarrowCprConfiguration, NarrowCprStrategy
from app.strategies.implementations.open_extreme import (
    OpenExtremeConfiguration,
    OpenHighStrategy,
    OpenLowStrategy,
)
from app.strategies.implementations.previous_session_body_pct import (
    PreviousSessionBodyPctConfiguration,
    PreviousSessionBodyPctStrategy,
)
from app.strategies.implementations.previous_session_range_pct import (
    PreviousSessionRangePctConfiguration,
    PreviousSessionRangePctStrategy,
)
from app.strategies.implementations.previous_session_relative_range import (
    PreviousSessionRelativeRangeConfiguration,
    PreviousSessionRelativeRangeStrategy,
)


class UnknownEnabledStrategyError(RuntimeError):
    """Raised when an enabled strategy id does not resolve from the catalog (fail closed)."""


@dataclass(frozen=True, slots=True)
class StrategyCatalogEntry:
    """One production strategy with its default configuration and optional scanner policy.

    Attributes:
        strategy: The concrete strategy plug-in.
        configuration: The default configuration (must match ``strategy.configuration_type``).
        ranking_policy: The cross-instrument scanner ranking policy, or ``None`` when the
            strategy is not scanner-ranked. Its ``strategy_id`` must equal the strategy's id.
    """

    strategy: Strategy
    configuration: StrategyConfiguration
    ranking_policy: ScannerRankingPolicy | None = None

    def __post_init__(self) -> None:
        """Validate that the strategy, its configuration, and its policy agree (REG2)."""
        strategy_id = self.strategy.descriptor.strategy_id
        if not isinstance(self.configuration, self.strategy.configuration_type):
            expected = self.strategy.configuration_type.__name__
            raise ValueError(f"configuration for {strategy_id!r} must be a {expected}")
        if self.ranking_policy is not None and self.ranking_policy.strategy_id != strategy_id:
            raise ValueError(
                f"ranking policy strategy_id {self.ranking_policy.strategy_id!r} "
                f"does not match strategy id {strategy_id!r}"
            )

    @property
    def strategy_id(self) -> str:
        """Return the entry's canonical strategy id."""
        return self.strategy.descriptor.strategy_id


class StrategyCatalog:
    """A deterministic, duplicate-free registry of production strategy catalog entries."""

    def __init__(self, entries: Iterable[StrategyCatalogEntry]) -> None:
        """Build the catalog, rejecting duplicate strategy ids fail-fast."""
        by_id: dict[str, StrategyCatalogEntry] = {}
        for entry in entries:
            if entry.strategy_id in by_id:
                raise ValueError(f"duplicate strategy id in catalog: {entry.strategy_id!r}")
            by_id[entry.strategy_id] = entry
        self._entries = by_id

    def resolve(self, enabled_ids: Sequence[str]) -> tuple[StrategyCatalogEntry, ...]:
        """Return the entries for ``enabled_ids`` in order, failing closed on any unknown id.

        Args:
            enabled_ids: The explicitly enabled strategy ids (``Settings.strategies_enabled``).

        Returns:
            The resolved catalog entries, one per id, in the given order.

        Raises:
            UnknownEnabledStrategyError: If any enabled id is not in the catalog.
        """
        resolved: list[StrategyCatalogEntry] = []
        for strategy_id in enabled_ids:
            entry = self._entries.get(strategy_id)
            if entry is None:
                raise UnknownEnabledStrategyError(
                    f"enabled strategy {strategy_id!r} is not in the production catalog"
                )
            resolved.append(entry)
        return tuple(resolved)


def production_catalog() -> StrategyCatalog:
    """Return the production strategy catalog (ADR-013 REG1/REG7).

    Each entry co-locates a strategy, its default configuration (no threshold — rank-all),
    and its scanner policy; the entry validates the policy id against the strategy descriptor,
    so a mismatched literal fails at construction. Narrow CPR ranks ``cpr_width_pct`` ascending
    (narrowest first); Previous Session Range % ranks ``previous_range_pct`` descending (largest
    previous-session range first); Previous Session Body % ranks ``previous_body_pct`` descending
    (largest absolute body first); Previous Session Relative Range ranks ``relative_range_ratio``
    ascending (most compressed vs its own 20-session baseline first). Open=High and Open=Low
    are current-session opening-structure plug-ins (ADR-009 CSOA22): each ranks
    ``session_range_pct`` descending (widest open-to-extreme travel first). Membership in the
    catalog does not enable any strategy — only ``Settings.strategies_enabled`` does, and the
    current-session pair additionally requires authoritative session statistics at runtime.
    """
    return StrategyCatalog(
        (
            StrategyCatalogEntry(
                strategy=NarrowCprStrategy(),
                configuration=NarrowCprConfiguration(config_version="1.0.0"),
                ranking_policy=ScannerRankingPolicy(
                    strategy_id="narrow_cpr",
                    metric_name="cpr_width_pct",
                    ordering=ScannerOrdering.ASCENDING,
                ),
            ),
            StrategyCatalogEntry(
                strategy=PreviousSessionRangePctStrategy(),
                configuration=PreviousSessionRangePctConfiguration(config_version="1.0.0"),
                ranking_policy=ScannerRankingPolicy(
                    strategy_id="previous_session_range_pct",
                    metric_name="previous_range_pct",
                    ordering=ScannerOrdering.DESCENDING,
                ),
            ),
            StrategyCatalogEntry(
                strategy=PreviousSessionBodyPctStrategy(),
                configuration=PreviousSessionBodyPctConfiguration(config_version="1.0.0"),
                ranking_policy=ScannerRankingPolicy(
                    strategy_id="previous_session_body_pct",
                    metric_name="previous_body_pct",
                    ordering=ScannerOrdering.DESCENDING,
                ),
            ),
            StrategyCatalogEntry(
                strategy=PreviousSessionRelativeRangeStrategy(),
                configuration=PreviousSessionRelativeRangeConfiguration(config_version="1.0.0"),
                ranking_policy=ScannerRankingPolicy(
                    strategy_id="previous_session_relative_range",
                    metric_name="relative_range_ratio",
                    ordering=ScannerOrdering.ASCENDING,
                ),
            ),
            StrategyCatalogEntry(
                strategy=OpenHighStrategy(),
                configuration=OpenExtremeConfiguration(config_version="1.0.0"),
                ranking_policy=ScannerRankingPolicy(
                    strategy_id="open_high",
                    metric_name="session_range_pct",
                    ordering=ScannerOrdering.DESCENDING,
                ),
            ),
            StrategyCatalogEntry(
                strategy=OpenLowStrategy(),
                configuration=OpenExtremeConfiguration(config_version="1.0.0"),
                ranking_policy=ScannerRankingPolicy(
                    strategy_id="open_low",
                    metric_name="session_range_pct",
                    ordering=ScannerOrdering.DESCENDING,
                ),
            ),
        )
    )
