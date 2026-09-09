"""Durable historical OHLCV store (DECOUPLING PHASE F).

Defines the broker-neutral :class:`HistoricalBarStore` Protocol and a durable file-backed
reference implementation for offline verification. Redis is NOT used for authoritative historical
OHLCV (it stays for streams / ephemeral / compacted reference state). A PostgreSQL-backed
implementation of the same Protocol is the intended production store, but it requires a new DB
schema + migration decision that is explicitly OUT OF SCOPE for Phase F (reported, not created).

Upserts are idempotent and fail closed on value conflicts: an existing bar is never silently
overwritten when the new values differ (returns CONFLICT), protecting against bad upstream data /
uncoordinated corporate-action rewrites.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import TypeAdapter

from app.market_history.models import HistoricalDailyBar, HistoricalSeries, PriceAdjustment

# TypeAdapter.validate_json / dump_json round-trips strict Decimal/datetime/enum losslessly from
# JSON text (a plain model_validate on a dict would reject str->Decimal under strict mode).
_BARS_ADAPTER: TypeAdapter[list[HistoricalDailyBar]] = TypeAdapter(list[HistoricalDailyBar])


class UpsertOutcome(StrEnum):
    """Deterministic result of upserting one bar."""

    INSERTED = "inserted"
    UNCHANGED = "unchanged"  # already stored with identical values (idempotent no-op)
    CONFLICT = "conflict"  # already stored with different values; existing kept, not overwritten


@runtime_checkable
class HistoricalBarStore(Protocol):
    """Durable per-instrument daily-bar store (consumers depend on this contract)."""

    def upsert(self, bar: HistoricalDailyBar) -> UpsertOutcome:
        """Insert one bar; idempotent on identical values, CONFLICT on differing values."""
        ...

    def upsert_many(self, bars: Iterable[HistoricalDailyBar]) -> dict[UpsertOutcome, int]:
        """Batch upsert; returns a count per outcome."""
        ...

    def read_range(
        self, instrument_identity: str, start: date, end: date, *, adjustment: PriceAdjustment
    ) -> HistoricalSeries:
        """Return the series for an inclusive date range."""
        ...

    def read_last_n(
        self, instrument_identity: str, *, as_of: date, n: int, adjustment: PriceAdjustment
    ) -> HistoricalSeries:
        """Return the most recent ``n`` bars on/before ``as_of``."""
        ...

    def available_dates(
        self, instrument_identity: str, *, adjustment: PriceAdjustment
    ) -> tuple[date, ...]:
        """Return all stored trading dates for an instrument (ascending)."""
        ...


class FileHistoricalBarStore:
    """File-backed durable :class:`HistoricalBarStore` (one JSON file per instrument+adjustment).

    Durability: PROCESS_RESTART_DURABILITY = yes (files persist; atomic temp+replace writes).
    HOST_DISK_LOSS_DURABILITY = no. Reference implementation for offline Phase-F verification —
    NOT a production database.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def upsert(self, bar: HistoricalDailyBar) -> UpsertOutcome:
        """Insert one bar into its instrument file; idempotent, fail-closed on a value conflict."""
        existing = self._load(bar.instrument_identity, bar.adjustment)
        by_date = {existing_bar.trading_date: existing_bar for existing_bar in existing}
        prior = by_date.get(bar.trading_date)
        if prior is not None:
            return UpsertOutcome.UNCHANGED if prior.has_same_values(bar) else UpsertOutcome.CONFLICT
        by_date[bar.trading_date] = bar
        self._write(bar.instrument_identity, bar.adjustment, by_date)
        return UpsertOutcome.INSERTED

    def upsert_many(self, bars: Iterable[HistoricalDailyBar]) -> dict[UpsertOutcome, int]:
        """Batch upsert grouped by instrument+adjustment (one read/write per group)."""
        counts: dict[UpsertOutcome, int] = {outcome: 0 for outcome in UpsertOutcome}
        grouped: dict[tuple[str, PriceAdjustment], list[HistoricalDailyBar]] = defaultdict(list)
        for bar in bars:
            grouped[(bar.instrument_identity, bar.adjustment)].append(bar)
        for (identity, adjustment), group in grouped.items():
            by_date = {b.trading_date: b for b in self._load(identity, adjustment)}
            dirty = False
            for bar in group:
                prior = by_date.get(bar.trading_date)
                if prior is None:
                    by_date[bar.trading_date] = bar
                    counts[UpsertOutcome.INSERTED] += 1
                    dirty = True
                elif prior.has_same_values(bar):
                    counts[UpsertOutcome.UNCHANGED] += 1
                else:
                    counts[UpsertOutcome.CONFLICT] += 1  # keep existing; never silent overwrite
            if dirty:
                self._write(identity, adjustment, by_date)
        return counts

    def read_range(
        self, instrument_identity: str, start: date, end: date, *, adjustment: PriceAdjustment
    ) -> HistoricalSeries:
        """Return the series for ``start <= trading_date <= end`` (inclusive)."""
        bars = tuple(
            bar
            for bar in self._load(instrument_identity, adjustment)
            if start <= bar.trading_date <= end
        )
        return HistoricalSeries(
            instrument_identity=instrument_identity, adjustment=adjustment, bars=bars
        )

    def read_last_n(
        self, instrument_identity: str, *, as_of: date, n: int, adjustment: PriceAdjustment
    ) -> HistoricalSeries:
        """Return the most recent ``n`` bars on/before ``as_of``."""
        eligible = tuple(
            bar for bar in self._load(instrument_identity, adjustment) if bar.trading_date <= as_of
        )
        selected = eligible[-n:] if n else ()
        return HistoricalSeries(
            instrument_identity=instrument_identity, adjustment=adjustment, bars=selected
        )

    def available_dates(
        self, instrument_identity: str, *, adjustment: PriceAdjustment
    ) -> tuple[date, ...]:
        """Return all stored trading dates for an instrument+adjustment (ascending)."""
        return tuple(bar.trading_date for bar in self._load(instrument_identity, adjustment))

    def _path(self, instrument_identity: str, adjustment: PriceAdjustment) -> Path:
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in instrument_identity)
        return self._root / f"{safe}__{adjustment.value}.json"

    def _load(
        self, instrument_identity: str, adjustment: PriceAdjustment
    ) -> tuple[HistoricalDailyBar, ...]:
        path = self._path(instrument_identity, adjustment)
        if not path.exists():
            return ()
        bars = _BARS_ADAPTER.validate_json(path.read_bytes())
        return tuple(sorted(bars, key=lambda bar: bar.trading_date))

    def _write(
        self,
        instrument_identity: str,
        adjustment: PriceAdjustment,
        by_date: dict[date, HistoricalDailyBar],
    ) -> None:
        ordered = [by_date[key] for key in sorted(by_date)]
        payload = _BARS_ADAPTER.dump_json(ordered)
        path = self._path(instrument_identity, adjustment)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
