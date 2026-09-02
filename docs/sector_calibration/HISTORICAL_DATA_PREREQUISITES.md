# Historical Data Prerequisites — Evidence (SECTOR-5B-PRE)

Evidence phase for the SECTOR-5B historical replay. **No Dhan authentication was performed;
0 historical requests were made.** Reason: the §3 gate requires a *safe non-production* Dhan
credential path, and none exists — see P10. Everything below is established from the ApexScan
adapter source and authoritative public documentation; nothing is live-verified against real
candles (that requires the missing credential path). Production untouched; ADR-016 unchanged.

## Access status

- No local Dhan credentials of any kind (no `.env`, no `DHAN*` env vars, no dhan config on
  the workstation). The only Dhan credentials are the **single production account** on host
  `65.2.105.7` (forbidden by §2/§3). ApexScan is a **single-account** setup (ADR-015): there
  is no separate diagnostic/dev identity, and token generation is **rate-limited** (the B10.3
  incident). Any auth with the production account would regenerate/expire the live token and
  risk taking production down — therefore **not attempted**.

## Provider capability (documented — NOT live-verified)

| item | value | source |
|------|-------|--------|
| Intraday intervals | {1, 5, 15, 25, 60} minutes | DhanHQ v2 historical docs + adapter `_SUPPORTED_INTRADAY_INTERVALS` |
| Intraday depth | "last 5 years" | DhanHQ v2 historical docs |
| Per-request range | ≤ 90 days per intraday call | DhanHQ v2 historical docs (`_INTRADAY_MAX_RANGE`) |
| Daily depth | since scrip inception | DhanHQ v2 historical docs |
| Timestamp format | epoch/UNIX seconds (v2) | DhanHQ v2 docs; adapter `_epoch_timestamp` = `fromtimestamp(int, tz=UTC)` |
| Daily corporate-action adjustment | **adjusted** for bonuses/splits | Dhan support (quoted below) |
| Intraday corporate-action adjustment | **not documented** | Dhan support addresses "Daily" only |

## A. 1-minute depth matrix — NOT PRODUCED (auth-blocked)

No probes were run (P10). The matrix (instrument × probe-age × retrieved?/bar-count/complete?)
must be produced in a re-run once a safe credential path exists. Sample **selection** was
prepared deterministically but not fetched: 3–5 liquid F&O underlyings from distinct primary
sectors (e.g. `NSE:RELIANCE` OIL_GAS_CONSUMABLE_FUELS, `NSE:HDFCBANK` FINANCIAL_SERVICES,
`NSE:INFY` INFORMATION_TECHNOLOGY, `NSE:MARUTI` AUTOMOBILE_AND_AUTO_COMPONENTS,
`NSE:SUNPHARMA` HEALTHCARE), identities resolvable via the SECTOR-2 dataset.

## B. Timestamp table — ApexScan canonical mapping (code) vs provider ground-truth (unverified)

| wire | ApexScan canonical (from `normalizer.py`) | eligible at T=09:30? | evidence |
|------|-------------------------------------------|----------------------|----------|
| epoch e | `start_timestamp = fromtimestamp(e, UTC)`; `end_timestamp = start + interval` | depends on start/end truth | adapter code (proven) |
| provider start/end meaning | **UNDOCUMENTED** (docs give only epoch format) | **UNVERIFIED** | Dhan v2 docs (silent); needs real candles |

**Adapter assumption:** the wire epoch is the candle **start** (left-labeled), so a 1-minute
bar with wire 09:29 is `[09:29, 09:30)`. **This assumption is not proven against provider
data.** If Dhan is right-labeled instead, the canonical `end_timestamp` is off by one interval
and the anti-lookahead boundary would be wrong — hence P3/P4 cannot PASS without live candles.

### 09:30 resolution (CONDITIONAL on the adapter's left-label assumption)

- **Last candle allowed in FEATURES_AT_T (09:30:00 IST):** the 1m bar with
  `end_timestamp = 09:30:00` → canonical `start = 09:29:00`, `end = 09:30:00` (`[09:29,09:30)`).
- **First FORBIDDEN candle:** `start = 09:30:00`, `end = 09:31:00` (`[09:30,09:31)`) — contains
  post-T data.
- Rule `end_timestamp ≤ T` is implementable from the canonical model **iff** the left-label
  assumption is confirmed. **Currently UNVERIFIED.**

## C. Data-quality table — NOT PRODUCED (auth-blocked)

Expected/returned/missing/duplicate/invalid-OHLC per session require live retrieval (P5
UNRESOLVED). Expected-bar counts will be derived from the authoritative `MarketSessionClassifier`
session bounds, compared to returned canonical intervals; no forward-fill, no synthesized bars,
missing minute ≠ unchanged stock.

## D. Corporate-action table

| scope | behaviour | source | verdict |
|-------|-----------|--------|---------|
| Daily historical | **adjusted** for bonuses/splits | Dhan support: *"Yes, the Daily Historical Data provided through Dhan's Data API is adjusted for corporate actions such as bonuses and splits."* | documented PASS (daily) |
| Intraday historical | not documented | Dhan support addresses daily only | UNRESOLVED (intraday) |

No specific corporate-action event was inspected against data (auth-blocked). Design stance
(from SECTOR-5A) holds: previous-close from **adjusted daily**; intraday ratios computed
**within a single session only** (open→T, T→T+h) so intraday adjustment status doesn't corrupt
horizon returns; any suspected unadjusted split (sanity-bound on overnight return) → excluded
`CORPORATE_ACTION_UNSAFE`. Never clip/replace/impute.

## E. F&O universe source table (point-in-time eligibility)

| source | authority | historical coverage | point-in-time? | underlying-level? | machine-readable? | usable? | limitation |
|--------|-----------|--------------------|----------------|-------------------|-------------------|---------|------------|
| NSE all-reports-derivatives / daily derivatives bhavcopy | NSE (official) | dated daily archives | **yes (assemblable)** | yes (traded contracts imply eligible underlyings) | yes (per-day files) | with assembly | must reconstruct eligibility per date from many daily files |
| NSE `fo_secban` / eligibility circulars | NSE (official) | dated | partial | yes | partial | supplementary | bans ≠ full eligibility list |
| NSE `historical_fo` page | NSE (official) | historical | partial | yes | partial | supplementary | legacy page |
| Current F&O list (NSE/broker) | NSE/broker | current only | **no** | yes | yes | pipeline-validation only | **survivorship-biased** |

**Universe verdict:** `POINT_IN_TIME_FNO_UNIVERSE_PARTIALLY_AVAILABLE` — dated eligibility is
*assemblable* from NSE daily derivatives archives but is **not currently held** by ApexScan and
requires a build step. Until assembled, replay uses `CURRENT_UNIVERSE_RETROSPECTIVE_ONLY`,
which **must** stay explicitly labeled survivorship-biased and **must not** become final
production calibration evidence (it may be used for pipeline/replay-correctness validation).

## P9 — Immutable source-acquisition design (two-stage; no implementation here)

- **FETCH stage** (network permitted, read-only, bounded, resumable, rate-limit-aware,
  dedup + local raw cache): download source data **once** into immutable raw research
  artifacts recording `source/provider, canonical identity, interval, requested range,
  retrieved_at, sanitized request metadata, record_count, sha256(content), source schema/version`.
  **Never** store tokens/PIN/TOTP/cookies/Authorization headers. Large market files are **not**
  committed to Git (gitignored raw cache).
- **COMPUTE stage** (no network): replay runs deterministically from stored raw artifacts +
  ApexScan source SHA + replay config + membership dataset + trading calendar + pinned tool
  versions ⇒ logically identical outputs (byte-identical Parquet **not** required unless the
  writer stack is pinned and tested). No `datetime.now()`, ordered iteration only.

## Verdicts (P1–P10)

| P | prerequisite | verdict | note |
|---|--------------|---------|------|
| P1 | 1-minute retrieval | **PARTIAL** | documented+implemented; not live-verified (auth-blocked) |
| P2 | historical depth | **PARTIAL** | docs: ~5yr intraday, 90-day chunks; not verified |
| P3 | bar timestamp semantics | **PARTIAL** | canonical mapping known; provider start/end undocumented & unverified — cannot PASS |
| P4 | completed-bar lookahead safety | **PARTIAL** | rule implementable, correctness depends on unproven P3 |
| P5 | missing-bar semantics | **UNRESOLVED** | needs live sessions |
| P6 | corporate-action adjustment | **PARTIAL** | daily adjusted (documented); intraday unstated |
| P7 | historical identity continuity | **UNRESOLVED** | security-id concept exists; rename/CA continuity unverified |
| P8 | point-in-time F&O universe | **PARTIAL** | assemblable from NSE archives; not currently held |
| P9 | immutable source acquisition design | **PASS** | two-stage design above |
| P10 | safe auth/provider access | **BLOCKED** | no safe non-production credential path (single account; production forbidden) |

**Implementation gate (§40):** requires P3=PASS ∧ P4=PASS ∧ P9=PASS ∧ P10=PASS.
Current: P9 PASS; P3/P4 PARTIAL; **P10 BLOCKED** ⇒ SECTOR-5B implementation **NOT authorized**.

## Resolution needed before SECTOR-5B

1. A **safe non-production Dhan credential path** (e.g. a dedicated Dhan Data-API
   subscription/identity separate from the trading account), **or** an explicit governed
   decision + maintenance window to use the single account without disrupting production —
   a governance call, out of scope here. This unblocks P1/P2/P5 and enables live P3/P4 proof.
2. With access: confirm the **bar-label convention** against real first-session candles
   (settles P3 → P4).
3. Assemble **point-in-time F&O eligibility** from NSE daily derivatives archives (P8) if
   unbiased calibration is required (else proceed current-universe-retrospective, labeled).

## Sources

- [DhanHQ v2 Historical Data API](https://dhanhq.co/docs/v2/historical-data/)
- [Dhan support — historical data adjusted for corporate actions](https://dhan.co/support/platforms/dhanhq-api/is-the-historical-data-from-dhan-s-data-api-adjusted-for-corporate-actions-like-bonuses-and-splits/)
- [NSE all reports — derivatives](https://www.nseindia.com/all-reports-derivatives)
- ApexScan adapter: `app/adapters/dhan/normalizer.py` (`_epoch_timestamp`, historical candle build), `app/adapters/dhan/adapter.py` (`load_historical_data`, `_SUPPORTED_INTRADAY_INTERVALS`, `_INTRADAY_MAX_RANGE`).
