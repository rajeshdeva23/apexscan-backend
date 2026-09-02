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

## R2.2 — same-account multi-token coexistence check (evidence, no auth performed)

Investigated whether an *additional* same-account Dhan access token could be generated for
read-only historical use while production's token stays valid (would move P10 → PASS). **Not
executed** — blocked before any authentication:

- **Multi-token support is NOT documented (current official Dhan material).** The v2
  Authentication doc has no statement permitting multiple simultaneously-active tokens; the one
  explicit constraint in the API-Key/consent flow is *"at any given point of time, only one
  token will be generated"* (leans **single**). `RenewToken` is documented as *"expires your
  current token and provides a new token"*. Fresh-generation invalidation of *other* tokens is
  **NOT_DOCUMENTED**. Classification: max-simultaneous-tokens = **NOT_DOCUMENTED**; RenewToken =
  DOCUMENTED_CURRENT; generation-invalidation = NOT_DOCUMENTED.
- **No safe credential path** to generate a test token regardless (no local Dhan credentials;
  production creds / `dhan-r4d.env` forbidden).
- Therefore ADR-015's "single active token" is **not contradicted** by evidence and stands; no
  ADR change. **P10 remains BLOCKED**; P3/P4 remain PARTIAL. 0 auth attempts, 0 requests,
  production untouched.

To unblock: a **dedicated non-production Dhan Data-API credential** remains the recommended
path (production-safe, no coexistence assumption needed).

## R3 — governed single-account closed-market execution (2026-09-02, 23:05–23:38 IST)

Executed the governed maintenance window (market authoritatively `market_closed`). Backend
restart policy set to `no`, backend stopped (freeing the production token), and an isolated
one-off probe container (`docker compose run --no-deps`, entrypoint = probe, creds via the
normal `dhan.env` env_file — **not** `dhan-r4d.env`) was run.

**Outcome: evidence NOT obtained — probe-construction error, not a provider/account limit.**
- `connect()` succeeded; a token generation occurred (the single §9-budgeted evidence auth).
- The probe called `connect()` but **not** `adapter.load_instruments()`, so the instrument
  master was never loaded → `load_historical_data(RELIANCE)` hit `reference is None` →
  `UnsupportedProviderRequestError` (adapter.py:721-722, fail-closed on unmapped instrument).
  `get_health()`/`/profile` returned `NormalizationError`.
- No historical bars retrieved; **no raw artifacts written** (`r3evidence/` empty); no secrets.
- Per §9 (max one evidence generation) the run was **not** retried.

**Production recovery: SUCCESS.** After a >30-min quiet interval, one controlled `docker start`
at 23:38:38 IST authenticated cleanly (`Application startup complete`, provider **healthy**, no
rate-limit), confirming the **governed single-account closed-market auth + recovery path works**
end-to-end. Restart policy restored to `unless-stopped`; 10-min soak stable (RestartCount 0,
CPU ~0.15%, no errors); image `bd0c67f`; observer ON / authority OFF / strategies empty /
trading disabled; B9 artifact SHA unchanged (`af348246…`).

**Verdicts unchanged for the blocking gates:** P1/P3/P4/P5 remain **PARTIAL/UNRESOLVED** (no
empirical bars). P10 remains **BLOCKED** for *historical read-only access* (the historical call
never succeeded), though the auth/recovery mechanics themselves functioned. SECTOR-5B stays
**not authorized** (hard gate P3∧P4∧P9∧P10 unmet).

**Fix for a future R3 re-run** (one corrected probe, one fresh window): after `connect()`, call
`await adapter.load_instruments()` before any `load_historical_data(...)`; skip/relax the
`get_health` profile normalization; then run the P1/P3/P4/P5 probes as designed. No adapter
change is warranted — the adapter fail-closed correctly on an unmapped instrument.

## R3.1 — corrected probe offline preflight (no auth, no network, no production)

Root cause confirmed from source (not just the R3 report): `DhanRestAdapter.connect()`
(adapter.py:300, docstring *"without making a provider request"*) creates HTTP clients only
and does **not** populate `self._references` (init empty, 219). `load_instruments()` (349)
fetches the master and sets `self._references = {Instrument: reference}` (367), returning the
canonical `Instrument` tuple. `load_historical_data` looks up `self._references.get(request
.instrument)` (720) and raises `UnsupportedProviderRequestError` when `None` (722) — the
intended fail-closed. R3 skipped `load_instruments()`, so resolution failed. The R3
`get_health`/`/profile` `NormalizationError` was the token-generation response failing to
parse at that instant (via `get_health → _request_api_json → get_access_token`), **not** an
adapter defect (the identical path succeeded on the R3 recovery). Classification: **PROBE
MISUSE** (call order + treating one token-gen failure as fatal); no adapter change warranted.

**Corrected call order (frozen):** `connect() → load_instruments() → resolve Instrument from
the returned tuple → load_historical_data(...)`. The `/profile` health step is **skipped** in
R3.2 (redundant — the historical call authenticates). RELIANCE is resolved *from*
`load_instruments()` output (exact canonical `Instrument` key), never a constructed guess or
hard-coded security id.

**Offline evidence tooling added** (`app/tools/sector_historical_probe/`, NOT imported by any
runtime): pure evaluators — `classify_bar_label` (LEFT/RIGHT/AMBIGUOUS/INVALID, requires
first-start==open **and** last-end==close for a verdict; a lone 09:15 → AMBIGUOUS, §12),
`feature_eligible` (`end ≤ T`), `resolve_instrument` (unique-or-None), `session_quality`
(missing/dup/invalid/out-of-session/non-monotonic; no fill/synthesis), `write_raw_artifact`
(sha256 + sanitized meta; rejects credential-like keys), and `run_probe` (corrected flow,
adapter injected via Protocol). Dry-run (`tests/unit/tools/test_sector_historical_probe.py`,
8 tests) with an httpx kill-switch proves: master-loaded → RELIANCE resolves → reaches the
historical-call boundary; **without** master → fail-closed `LookupError`, no historical call;
evaluators behave; **network calls = 0**. Mocks are not provider evidence — no prerequisite
verdict is upgraded (P1..P10 unchanged).

### R3.2 request manifest (proposed; ≤10 historical/reference calls, 1 research auth)

| # | purpose | call | instrument | interval | date/range | evidence |
|---|---------|------|-----------|----------|-----------|----------|
| — | instrument master (unauth) | `load_instruments()` | all | — | current | resolve RELIANCE |
| 1 | P1/P3/P4/P5 full session | `load_historical_data` (1st ⇒ token gen ×1) | RELIANCE | 1m | latest completed normal session (calendar-resolved, **strictly before** exec date), 09:15–15:30 IST | bars, first/last ts, 09:30 boundary, missing/dup/invalid |
| 2 | depth ~30d | `load_historical_data` | RELIANCE | 1m | ~30 cal-days back, 09:15–09:20 | retrievability |
| 3 | depth ~90d | " | RELIANCE | 1m | ~90d back window | retrievability |
| 4 | depth ~180d | " | RELIANCE | 1m | ~180d back window | retrievability |
| 5 | depth ~365d | " | RELIANCE | 1m | ~365d back window | oldest verified depth |
| 6 | prev-close / daily | `load_historical_data` | RELIANCE | 1 day | ~8 completed days | daily semantics + prev-close source |

Total: 1 unauth master + 6 historical = within the ≤10 target / 20 ceiling. **R3.2 auth budget:**
research token generation ×1, production-recovery auth ×1; no retry, no RenewToken, no
concurrent-token experiment. R3.2 retains the governed maintenance rule (MARKET_CLOSED,
restart-policy off, graceful stop, ≥30-min quiet before the single controlled recovery,
restore policy after health, post-recovery soak). Sample-date selection is deterministic:
the latest trading day strictly before the R3.2 execution date per the authoritative calendar.

## Sources

- [DhanHQ v2 Historical Data API](https://dhanhq.co/docs/v2/historical-data/)
- [Dhan support — historical data adjusted for corporate actions](https://dhan.co/support/platforms/dhanhq-api/is-the-historical-data-from-dhan-s-data-api-adjusted-for-corporate-actions-like-bonuses-and-splits/)
- [NSE all reports — derivatives](https://www.nseindia.com/all-reports-derivatives)
- ApexScan adapter: `app/adapters/dhan/normalizer.py` (`_epoch_timestamp`, historical candle build), `app/adapters/dhan/adapter.py` (`load_historical_data`, `_SUPPORTED_INTRADAY_INTERVALS`, `_INTRADAY_MAX_RANGE`).
