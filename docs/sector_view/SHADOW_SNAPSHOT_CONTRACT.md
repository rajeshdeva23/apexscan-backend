# Shadow Snapshot Contract (SECTOR-VIEW-1B)

`app/services/sector_intelligence/snapshot.py` — `SectorShadowSnapshot` (frozen, immutable).
`schema_version = "sector-shadow-1"`.

Carries only **raw** SECTOR-3/4 evidence plus completeness/diagnostic counts. It contains **no**
`SectorScore`, no confidence, no `STRONG_*` sector labels, and no
`LEADER/PARTICIPANT/LAGGARD/DIVERGENT` stock labels — those are calibrated production concepts
(SECTOR-5C/5D), not shadow output.

## Fields

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | str | `"sector-shadow-1"` |
| `trading_date` | date \| None | Session date the state belongs to; `None` before any session established |
| `evaluation_timestamp` | datetime | Clock time the evaluation began |
| `expected_universe_count` | int | Size of the SECTOR-2 expected universe (dynamic; never hardcoded) |
| `observed_count` | int | Instruments with a stored observation (≤ expected) |
| `complete_count` | int | Observations with all inputs present for the session |
| `fresh_count` | int | Complete observations within the freshness limit (from universe proxy) |
| `stale_count` | int | `complete_count − fresh_count` |
| `missing_previous_close_count` | int | Observations with `previous_close = None` |
| `missing_session_open_count` | int | Observations with `session_open = None` |
| `missing_last_price_count` | int | Observations with `last_price = None` |
| `other_incomplete_count` | int | All prices present but not usable this session (missing/mismatched trading date) |
| `universe_proxy` | UniverseProxyMetrics \| None | SECTOR-3 equal-weight F&O proxy; `None` before a session |
| `sector_metrics` | tuple[SectorMetrics, …] | Verbatim SECTOR-3 output, one per primary sector |
| `stock_rankings` | tuple[SectorStockRanking, …] | Verbatim SECTOR-4 output, one per primary sector |
| `runtime_diagnostics` | ShadowDiagnosticsView | Bounded counters snapshot |

The missing-field counts are independent (an observation missing two fields is counted in two
of them). `complete_count` counts observations usable for the session; `other_incomplete_count`
captures the remainder that have all prices but a missing/mismatched trading date.

## Allowed sector output (SECTOR-3, raw)

`sector_id`, `expected_count`, `valid_count`, `stale_count`, `coverage_ratio`, overnight /
intraday / total median returns, breadth, dispersion (MAD, IQR), `relative_strength`, and
`raw_direction` (`BULLISH/BEARISH/NEUTRAL/MIXED/INSUFFICIENT_DATA`). No score, no confidence,
no `STRONG_*`.

## Allowed stock output (SECTOR-4, raw)

Per `SectorStockRanking`: identity, `sector_id`, intraday return, `stock_vs_sector`,
`stock_vs_universe`, alignment, directional strength, within-sector rank + percentile, robust
relative magnitude, `eligible`, and `exclusion_reason`. Directional rank/percentile appear only
when the sector raw direction is `BULLISH`/`BEARISH`. No production labels.

## Diagnostics (`ShadowDiagnosticsView`)

`events_received`, `events_accepted`, `events_rejected`, `unknown_instruments`,
`duplicate_events`, `out_of_order_events`, `late_trading_date_events`, `rollovers`,
`snapshot_attempts`, `snapshot_successes`, `snapshot_failures`, `evaluation_overruns`,
`last_evaluation_duration_ms`, `last_success_timestamp`. Bounded counters only — no unbounded
history, no per-tick log.

## Access

`SectorShadowRuntime.latest_snapshot()` returns the latest **successful** immutable snapshot (or
`None`). `SectorShadowRuntime.diagnostics()` returns an immutable counters copy. Neither exposes
mutable internal state; there is no public API, EventBus publication, or persistence.
