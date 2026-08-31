# ADR-014 — Evidence Price Canonicalization & Resumable Collection (DEPLOY-10 R4C)

| Field | Value |
|-------|-------|
| **Status** | Proposed (evidence-tool semantics only; flips no authority bit, enables no strategy) |
| **Date** | 2026-08-31 |
| **Deciders** | Provider Evidence / Platform Architecture |
| **Complements** | ADR-008 (authoritative provider-supplied current-session statistics), ADR-008 tick-aggregate evidence procedure (DEPLOY-10 R2), ADR-009 (REST-backed authoritative session statistics; CSOA enable path) |
| **Related** | DEPLOY-10 R4A/R4B evidence tool; CSOA9, CSOA16, CSOA20, CSOA22 |

---

## Context

Accepted ADR-008 is immutable; this ADR adds only the *evidence-tool* comparison and
collection semantics and does not edit it. The 2026-08-31 R4B live collection returned
`REJECTED` for 43 "open mismatches" that were all of the form WS
`Decimal("212.3699951171875")` vs REST `Decimal("212.37")` — the **same price**. The Dhan
live feed carries session OHLC as IEEE-754 **32-bit** floats (`<f` in the quote/full packet
structs, `app/adapters/dhan/live.py:38-39`); the adapter decodes via `Decimal(str(float(...)))`
(`live.py:482-485`), widening float32 to binary64 and printing its full expansion. REST returns
a clean 2-decimal `Decimal`. Exact `Decimal` equality reported a false mismatch. The verdict was
correct given the rule; the rule was too strict.

R4B also surfaced operational collection weaknesses: partial universe coverage in a bounded
window, no resumable early/mid/late workflow, and no way to capture late-start / reconnect
(CSOA16) continuity evidence.

## Decision

### D1 — Protocol-representation equivalence (not rounding, not a universal tick)
Two prices are **protocol-equivalent** iff they encode to the identical 4-byte IEEE-754 float32
wire representation (`struct.pack("<f", float(value))`). This is the provider's own on-the-wire
form: no arbitrary decimal rounding, no universal precision, no universal tick.

- A price and its widened-binary64 expansion collapse to the same float32 (true match).
- Genuinely different prices round to different float32 and remain MISMATCH — including
  sub-paisa (`100.001` vs `100.002` are **not** collapsed).
- Values with no canonical float32 form fail closed (MISMATCH): non-finite (NaN/±Inf) and
  finite-but-out-of-float32-range values (e.g. `1e100`, which overflows `struct.pack("<f")`).

Rejected alternatives: round both sides to 2 dp (assumes universal precision, hides sub-paisa
disagreement); a universal 0.05 tick on open (open must be exact; masks real open mismatch);
`str()` comparison (the artifact's cause).

### D2 — Classification order (`classify_price`)
missing/non-finite → MISMATCH; exact `Decimal` equality → MATCH; **float32-equivalent →
PROTOCOL_EQUIVALENT**; then open exact-only → MISMATCH; high/low unknown tick → INDETERMINATE;
high/low within recorded authoritative tick → DRIFT; else MISMATCH. Protocol-equivalence is
resolved *before* any tick handling and is a true match (does not block ACCEPT). Unknown tick
remains `None` and non-equivalent high/low differences stay INDETERMINATE — no `0.05` fallback.

### D3 — Auditability (schema 2.1.0)
Each `OracleComparison` records `method` (exact/float32/tick/unknown_tick/none), both
`ws_float32_bits` / `rest_float32_bits`, `ws_value`, `rest_value`, `tick_size|None`, and
`classification`, so every PROTOCOL_EQUIVALENT decision is reproducible from the artifact.
Schema-2.1.0 fields are additive with defaults; schema-2.0.0 R4B records still validate, and
re-evaluation re-derives from the *stored* classifications, so the 2026-08-31 record remains
`REJECTED`. This ADR invokes no artifact migration.

### D4 — Coverage & resumable collection
The collector accumulates identity coverage across the window, stops early on full coverage,
and stops at a deterministic per-window deadline (`--per-window-seconds`, operator-configurable,
not hard-coded); unobserved expected identities are persisted as `pending_instruments`.
`combine_records` (CLI `combine`) merges per-window partial records for one session so an
operator can collect early/mid/late in separate bounded runs and merge later. It refuses to
span sessions (differing `trading_date` / `session_identity` / `source_sha`) and refuses
overlapping sample windows (a window combined twice), and recomputes coverage, pending, and
windows from the union.

### D5 — CSOA16 continuity capture
`capture-late-start` records a pre-subscription REST snapshot (`prior_*`) and the first
post-subscription WS observation (`first_*`); `capture-reconnect` records a `pre` observation
and a `post` observation taken across a fresh diagnostic socket (a reconnect). Both are bounded
by a deadline and close their diagnostic stream cleanly; the evaluator derives continuity from
the raw values. On combine, late-start keeps the earliest capture and reconnect spans
earliest-pre → latest-post across all observed reconnects, so a later loss cannot be masked by
an earlier good capture.

### D6 — Isolation & oracle limitation
Capture uses only the CLI's own `DhanRestAdapter` (its own connect/stream/disconnect); it never
touches the running production feed socket, container, env, Redis, DB, authority, or strategy
state. `oracle_source` stays `dhan_rest_marketfeed_ohlc` and `oracle_independent=False`:
float32 protocol equivalence proves cross-path representation consistency, not independent
external ground truth.

## Consequences

Enablement remains a governed composition change (ADR-009 CSOA20): this ADR changes no
authority bit and adds no operator flag. Acceptance still requires a live evidence record that
evaluates `ACCEPTED` with full identity coverage, all required windows, and observed CSOA16
continuity, collected under a separately authorized live session (future R4D) — not by this
offline correction.
