"""Read-only sector-shadow diagnostics projection + capability seam (SECTOR-VIEW-1D).

Projects the SectorShadowRuntime's existing latest-good snapshot and bounded counters into a
JSON response. It performs NO sector math — it only reads and reshapes what the runtime already
computed. Raw SECTOR-3/4 fields are surfaced verbatim (no SectorScore/confidence/labels exist to
leak). Decimals serialize as strings; values stay ``null`` where mathematically unavailable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.adapters.base.live_feed_diagnostics import LiveFeedDecodeDiagnostics
from app.market_engine.tick_diagnostics import TickEngineDiagnostics
from app.services.sector_intelligence import SectorShadowSnapshot, ShadowDiagnosticsView

MAX_STOCK_LIMIT = 50


@runtime_checkable
class SectorShadowDiagnosticsSource(Protocol):
    """Read-only seam the diagnostics endpoint narrows the lifecycle provider to."""

    def sector_shadow_read_available(self) -> bool:
        """Whether a shadow-diagnostics read is safe (composed + started)."""
        ...

    def sector_shadow_snapshot(self) -> SectorShadowSnapshot | None:
        """The latest-good shadow snapshot, or ``None``."""
        ...

    def sector_shadow_diagnostics(self) -> ShadowDiagnosticsView | None:
        """The bounded shadow diagnostics, or ``None`` when disabled."""
        ...

    def live_feed_decode_diagnostics(self) -> LiveFeedDecodeDiagnostics | None:
        """The bounded live-frame decode diagnostics, or ``None``."""
        ...

    def tick_engine_diagnostics(self) -> TickEngineDiagnostics | None:
        """The bounded TickEngine accept/reject diagnostics, or ``None``."""
        ...


class SectorShadowDiagnosticsResponse(BaseModel):
    """The read-only diagnostics payload (bounded; no sector math performed here)."""

    enabled: bool
    trading_date: str | None
    runtime: dict[str, Any] | None
    universe: dict[str, Any] | None
    coverage: dict[str, Any] | None
    sector_summary: dict[str, Any] | None
    sectors: list[dict[str, Any]]
    stock_participation: list[dict[str, Any]]
    live_feed: dict[str, Any] | None
    tick_engine: dict[str, Any] | None = None


def _ratio(numerator: int, denominator: int) -> float | None:
    """Coverage ratio, or ``None`` when the denominator is zero (unavailable, not zero)."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _universe(snapshot: SectorShadowSnapshot) -> dict[str, Any]:
    return {
        "expected_instruments": snapshot.expected_universe_count,
        "observed_instruments": snapshot.observed_count,
        "complete_instruments": snapshot.complete_count,
        "fresh_instruments": snapshot.fresh_count,
        "stale_instruments": snapshot.stale_count,
        "last_price_count": snapshot.observed_count - snapshot.missing_last_price_count,
        "previous_close_count": snapshot.observed_count - snapshot.missing_previous_close_count,
        "session_open_count": snapshot.observed_count - snapshot.missing_session_open_count,
        "missing_previous_close_count": snapshot.missing_previous_close_count,
        "missing_session_open_count": snapshot.missing_session_open_count,
        "missing_last_price_count": snapshot.missing_last_price_count,
        "other_incomplete_count": snapshot.other_incomplete_count,
    }


def _coverage(snapshot: SectorShadowSnapshot) -> dict[str, Any]:
    expected = snapshot.expected_universe_count
    observed = snapshot.observed_count
    return {
        "observed_coverage_ratio": _ratio(observed, expected),
        "complete_coverage_ratio": _ratio(snapshot.complete_count, expected),
        "fresh_coverage_ratio": _ratio(snapshot.fresh_count, expected),
        "previous_close_coverage_ratio": _ratio(
            observed - snapshot.missing_previous_close_count, expected
        ),
        "session_open_coverage_ratio": _ratio(
            observed - snapshot.missing_session_open_count, expected
        ),
    }


def _sector_summary(snapshot: SectorShadowSnapshot) -> dict[str, Any]:
    directions = {"bullish": 0, "bearish": 0, "neutral": 0, "mixed": 0, "insufficient_data": 0}
    for metrics in snapshot.sector_metrics:
        directions[metrics.raw_direction.value] = directions.get(metrics.raw_direction.value, 0) + 1
    insufficient = directions.get("insufficient_data", 0)
    total = len(snapshot.sector_metrics)
    return {
        "sector_count": total,
        "sufficient_sector_count": total - insufficient,
        "insufficient_sector_count": insufficient,
        "bullish": directions["bullish"],
        "bearish": directions["bearish"],
        "neutral": directions["neutral"],
        "mixed": directions["mixed"],
        "insufficient": insufficient,
    }


def _stock_participation(
    snapshot: SectorShadowSnapshot, *, sector: str | None, limit: int | None
) -> list[dict[str, Any]]:
    cap = MAX_STOCK_LIMIT if limit is None else min(limit, MAX_STOCK_LIMIT)
    rows: list[dict[str, Any]] = []
    for ranking in snapshot.stock_rankings:
        if sector is not None and ranking.sector_id != sector:
            continue
        for stock in ranking.ranked_stocks:
            if len(rows) >= cap:
                return rows
            row = stock.model_dump(mode="json")
            row["sector_id"] = ranking.sector_id
            rows.append(row)
    return rows


def project(
    *,
    snapshot: SectorShadowSnapshot | None,
    diagnostics: ShadowDiagnosticsView | None,
    live_feed: LiveFeedDecodeDiagnostics | None,
    tick_engine: TickEngineDiagnostics | None = None,
    sector: str | None = None,
    limit: int | None = None,
) -> SectorShadowDiagnosticsResponse:
    """Project the runtime's existing state into the response (no recomputation)."""
    runtime = diagnostics.model_dump(mode="json") if diagnostics is not None else None
    live = _live_feed_dump(live_feed)
    engine = _tick_engine_dump(tick_engine)
    if snapshot is None:
        return SectorShadowDiagnosticsResponse(
            enabled=diagnostics is not None,
            trading_date=None,
            runtime=runtime,
            universe=None,
            coverage=None,
            sector_summary=None,
            sectors=[],
            stock_participation=[],
            live_feed=live,
            tick_engine=engine,
        )
    return SectorShadowDiagnosticsResponse(
        enabled=True,
        trading_date=snapshot.trading_date.isoformat() if snapshot.trading_date else None,
        runtime=runtime,
        universe=_universe(snapshot),
        coverage=_coverage(snapshot),
        sector_summary=_sector_summary(snapshot),
        sectors=[metrics.model_dump(mode="json") for metrics in snapshot.sector_metrics],
        stock_participation=_stock_participation(snapshot, sector=sector, limit=limit),
        live_feed=live,
        tick_engine=engine,
    )


def _str_keys(table: dict[int, int]) -> dict[str, int]:
    """Stringify integer response-code keys for JSON."""
    return {str(key): value for key, value in table.items()}


def _live_feed_dump(live_feed: LiveFeedDecodeDiagnostics | None) -> dict[str, Any] | None:
    if live_feed is None:
        return None
    return {
        "websocket_messages_total": live_feed.websocket_messages_total,
        "framing_failures": live_feed.framing_failures,
        "provider_packets_total": live_feed.frames_total,
        "frames_total": live_feed.frames_total,
        "decoded_total": live_feed.decoded_total,
        "failures_total": live_feed.failures_total,
        "normalization_failures": live_feed.normalization_failures,
        "unsupported_code_failures": live_feed.unsupported_code_failures,
        "unknown_reference_failures": live_feed.unknown_reference_failures,
        "non_binary_frames": live_feed.non_binary_frames,
        "previous_close_frames_seen": live_feed.previous_close_frames_seen,
        "previous_close_frames_failed": live_feed.previous_close_frames_failed,
        "frames_by_response_code": _str_keys(live_feed.frames_by_response_code),
        "failures_by_response_code": _str_keys(live_feed.failures_by_response_code),
        "failures_by_length_bucket": dict(live_feed.failures_by_length_bucket),
    }


def _iso(moment: object) -> str | None:
    """Serialize a datetime to ISO-8601, or ``None`` when absent."""
    return moment.isoformat() if isinstance(moment, datetime) else None


def _tick_engine_dump(tick_engine: TickEngineDiagnostics | None) -> dict[str, Any] | None:
    if tick_engine is None:
        return None
    return {
        "ticks_received": tick_engine.ticks_received,
        "ticks_accepted": tick_engine.ticks_accepted,
        "ticks_rejected_total": tick_engine.ticks_rejected_total,
        "rejected_by_reason": dict(tick_engine.rejected_by_reason),
        "references_received": tick_engine.references_received,
        "references_accepted": tick_engine.references_accepted,
        "references_rejected": tick_engine.references_rejected,
        "last_accepted_event_timestamp": _iso(tick_engine.last_accepted_event_timestamp),
        "last_rejected_reason": tick_engine.last_rejected_reason,
        "last_rejected_event_timestamp": _iso(tick_engine.last_rejected_event_timestamp),
        "last_rejection_observed_at": _iso(tick_engine.last_rejection_observed_at),
        "last_rejected_event_clock_delta_seconds": (
            tick_engine.last_rejected_event_clock_delta_seconds
        ),
    }
