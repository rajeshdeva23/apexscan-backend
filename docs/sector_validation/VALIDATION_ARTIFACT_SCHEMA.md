# Validation Artifact Schema (SECTOR-VALIDATION-1)

The evidence artifact produced by `app.tools.sector_validation.to_artifact(UniverseEvaluation)`.
It is a **read-only evidence** rendering of the harness output — **not** the production
`SectorSnapshot` runtime contract (that is a later phase). No SectorScore, confidence, or
final classifications. Decimals are serialized as strings; unavailable values are `null`.

## Top level

| field | type | note |
|-------|------|------|
| `schema_version` | str | `"sector-validation-1"` |
| `trading_date` | str (ISO date) | evaluation trading date |
| `evaluation_timestamp` | str (ISO datetime, tz-aware) | the T used for freshness/labels |
| `expected_universe_count` | int | Σ SECTOR-2 primary members (e.g. 210) |
| `observed_count` | int | observations supplied |
| `mapped_count` | int | observations resolving to a primary sector |
| `valid_count` | int | fresh + valid contributing to metrics |
| `stale_count` | int | excluded as stale |
| `unmapped_identities` | str[] | observations with no primary membership (excluded) |
| `universe_proxy_intraday_return` | str\|null | equal-weight median intraday (F&O proxy, not NIFTY) |
| `sectors` | object[] | one per primary sector |

## Per-sector

| field | type | note |
|-------|------|------|
| `sector_id` | str | primary sector key |
| `member_count` | int | SECTOR-2 expected members |
| `valid_count` | int | fresh valid constituents |
| `coverage_ratio` | str | valid / member_count |
| `median_intraday_return` | str\|null | SECTOR-3 median (authority for direction) |
| `net_breadth` | str\|null | (adv−dec)/valid ∈ [−1,1] |
| `mad_intraday_return` | str\|null | robust dispersion |
| `iqr_intraday_return` | str\|null | Tukey exclusive hinges; null for N<2 |
| `relative_strength` | str\|null | median − universe proxy |
| `raw_direction` | str | BULLISH/BEARISH/NEUTRAL/MIXED/INSUFFICIENT_DATA |
| `directional_ranking_available` | bool | true only for BULLISH/BEARISH |
| `stocks` | object[] | ranked (eligible) then excluded |

## Per-stock

| field | type | note |
|-------|------|------|
| `identity` | str | canonical `NSE:SYMBOL` |
| `intraday_return` | str | since-open |
| `stock_vs_sector` | str\|null | − sector median |
| `stock_vs_universe` | str\|null | − universe proxy |
| `alignment` | str | ALIGNED/OPPOSED/NEUTRAL/MIXED_CONTEXT/INSUFFICIENT_DATA |
| `within_sector_rank` | int\|null | competition rank; null when non-directional |
| `within_sector_percentile` | str\|null | (N−rank)/(N−1); null for N<2 or non-directional |
| `eligible` | bool | false ⇒ excluded (stale) |
| `exclusion_reason` | str\|null | e.g. `stale` |

## Storage & secrets

Live-generated artifacts are large → **outside Git** (bind-mounted path, like R4D). Committed:
schemas, small fixtures, docs, and the pure harness. Artifacts contain market prices +
canonical identities only — **never** tokens/PIN/TOTP/Authorization/cookies. Secret-scan
expectation: 0.
