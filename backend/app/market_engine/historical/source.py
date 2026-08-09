"""Broker-neutral historical-source port, fetch plan, and result verification (P4.5B).

The Market Engine reaches historical data only through :class:`HistoricalSource`,
a port defined *inside* the engine so the engine never imports a concrete adapter
or broker SDK (ADR-003; docs/05 §5.6). A composition bridge outside the engine
wires a concrete provider (e.g. the Phase-3 ``HistoricalDataAdapter``) to this
port. The canonical, already broker-neutral ``HistoricalRequest``/``HistoricalResult``
contracts are reused rather than duplicated.

A :class:`HistoricalFetchPlan` is the deterministic, provider-agnostic unit of
work — one instrument, one requirement, and a resolved timezone-aware window; its
:class:`HistoricalRequestKey` drives both caching and in-flight deduplication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from app.market_engine.historical.context import HistoricalSeries
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, HistoricalRequest, HistoricalResult, Instrument

_SESSION_INTERVAL = timedelta(days=1)


class HistoricalSourceError(RuntimeError):
    """A provider-neutral failure surfaced by a historical source to the engine.

    Concrete provider transport/rate-limit errors are translated to this neutral
    failure by the composition bridge; the Market Engine never sees provider codes.
    """


class HistoricalDataQualityError(ValueError):
    """Raised when a historical source returns malformed (non-authoritative) data."""


def interval_for_timeframe(timeframe: Timeframe) -> timedelta:
    """Map a timeframe to the canonical request interval a source expects.

    Intraday timeframes map to their own duration; the whole-session timeframe
    maps to the daily interval (docs/05 daily historical endpoint; §24 mapping).

    Args:
        timeframe: The timeframe to map.

    Returns:
        The positive :class:`~datetime.timedelta` interval for a request.
    """
    duration = timeframe.duration
    if duration is None:
        return _SESSION_INTERVAL
    return duration


@dataclass(frozen=True, slots=True)
class HistoricalRequestKey:
    """The broker-neutral identity of one unit of historical work.

    Two plans that would fetch the same data map to the same key, so the cache
    and in-flight deduplication treat them as one. It carries no consumer key,
    strategy name, or provider identifier.

    Attributes:
        instrument: The canonical instrument.
        timeframe: The requested timeframe.
        start: The window start (timezone-aware UTC).
        end: The window end (timezone-aware UTC).
    """

    instrument: Instrument
    timeframe: Timeframe
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class HistoricalFetchPlan:
    """A deterministic, provider-agnostic plan to fetch one requirement's window.

    Attributes:
        instrument: The instrument the history is for.
        requirement: The effective requirement (timeframe + lookback) driving it.
        start: The resolved window start (timezone-aware UTC).
        end: The resolved window end (timezone-aware UTC).
        interval: The canonical request interval for the timeframe.
    """

    instrument: Instrument
    requirement: HistoricalRequirement
    start: datetime
    end: datetime
    interval: timedelta

    @property
    def key(self) -> HistoricalRequestKey:
        """Return the broker-neutral cache/dedup identity for this plan."""
        return HistoricalRequestKey(
            instrument=self.instrument,
            timeframe=self.requirement.timeframe,
            start=self.start,
            end=self.end,
        )

    @property
    def request(self) -> HistoricalRequest:
        """Return the canonical historical request this plan issues to a source."""
        return HistoricalRequest(
            instrument=self.instrument,
            start_timestamp=self.start,
            end_timestamp=self.end,
            interval=self.interval,
        )


@runtime_checkable
class HistoricalSource(Protocol):
    """The engine-local capability for loading authoritative historical candles."""

    @property
    def direct_timeframes(self) -> frozenset[Timeframe]:
        """Return the timeframes this source can fetch directly (no reconstruction)."""
        ...

    async def load(self, request: HistoricalRequest) -> HistoricalResult:
        """Load authoritative candles for one canonical request.

        Raises:
            HistoricalSourceError: On any provider-neutral failure.
        """
        ...


def verify_source_candles(
    plan: HistoricalFetchPlan, result: HistoricalResult
) -> tuple[Candle, ...]:
    """Return authoritative, ordered candles from a source result, or raise if malformed.

    Reuses :class:`HistoricalSeries` as the identity boundary (single instrument,
    chronological order, no duplicate or overlapping intervals). For intraday
    timeframes each candle's width must equal the requested interval. An empty
    result is not malformed — it is returned as ``()`` for the caller to treat as
    unresolved (never fabricated).

    Args:
        plan: The plan the result answers.
        result: The candles returned by the source.

    Returns:
        The authoritative candles ordered oldest-first (possibly empty).

    Raises:
        HistoricalDataQualityError: If the result is malformed.
    """
    candles = result.candles
    if any(candle.instrument != plan.instrument for candle in candles):
        raise HistoricalDataQualityError("historical result contains a foreign instrument")
    if not candles:
        return ()
    try:
        series = HistoricalSeries(timeframe=plan.requirement.timeframe, candles=candles)
    except ValidationError as error:
        raise HistoricalDataQualityError("historical result failed identity validation") from error
    ordered = series.candles
    if not plan.requirement.timeframe.is_session and any(
        candle.end_timestamp - candle.start_timestamp != plan.interval for candle in ordered
    ):
        raise HistoricalDataQualityError("candle width does not match the requested timeframe")
    return ordered
