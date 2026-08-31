# ADR-008 Addendum — Evidence Price Canonicalization & Resumable Collection (DEPLOY-10 R4C)

Status: Proposed (evidence-tool correction only). Flips no authority bit, enables no
strategy, changes no production adapter, collects no new live session. Refines the
comparison rule in ADR-008 §A3 ("Decimal exact compare after canonical scale-normalisation")
after the 2026-08-31 R4B collection returned REJECTED for a reason that was an artifact of
the evidence tool, not of the provider data.

## R1 — What R4B actually showed
The 2026-08-31 live collection reported 43 open "mismatches". Inspection of the raw record
showed every one was of the form:

- WS `Tick.session_ohlc.open` = `Decimal("212.3699951171875")`
- REST `/marketfeed/ohlc` open = `Decimal("212.37")`

These are the **same price**. The Dhan live feed carries session OHLC as IEEE-754 **32-bit**
floats (`<f` in the quote/full packet structs, `app/adapters/dhan/live.py`). The adapter
decodes each via `Decimal(str(float(...)))`, which widens the float32 to binary64 and prints
its full decimal expansion (`212.37` → `212.3699951171875`). REST returns a clean 2-decimal
`Decimal`. Exact `Decimal` equality therefore reported a false mismatch. The REJECTED verdict
was correct given the tool's rule; the rule was too strict.

## R2 — Rejected fixes (and why)
- **Round both sides to 2 dp.** Rejected: assumes a universal 2-decimal price precision. NSE
  quotes some instruments to finer precision; rounding would hide genuine sub-paisa
  disagreements and is an unproven assumption, not a canonical form.
- **Allow a universal 0.05 tick on open.** Rejected: open must be exact (ADR-008 §A3,
  tolerance = 0 for open). A tick allowance on open would mask a real open mismatch, and
  0.05 is not the tick for every instrument.
- **Compare `str()` forms.** Rejected: `str(Decimal)` is not canonical across the two
  encodings; it is exactly what produced the artifact.

## R3 — Adopted rule: protocol-representation equivalence
Two prices are **protocol-equivalent** iff they encode to the identical 4-byte IEEE-754
float32 wire representation (`struct.pack("<f", float(value))`). This is the provider's own
on-the-wire form, so it introduces no arbitrary tolerance and no precision assumption:

- A price and its widened-binary64 expansion collapse to the same float32 (true match).
- Two genuinely different prices round to different float32 values and remain a MISMATCH —
  including sub-paisa differences (`100.001` vs `100.002` are **not** collapsed).
- Non-finite values (NaN, ±Inf) have no canonical wire form and are always MISMATCH
  (fail closed).

Classification order in `classify_price` (`app/tools/session_ohlc_evidence/evaluate.py`):
missing/non-finite → MISMATCH; exact `Decimal` equality → MATCH; **float32-equivalent →
PROTOCOL_EQUIVALENT**; then (open) exact-only → MISMATCH, (high/low) unknown tick →
INDETERMINATE, within recorded tick → DRIFT, else MISMATCH. PROTOCOL_EQUIVALENT is a true
match (does not block ACCEPT); it is counted and reported separately, and each comparison
records `method` plus both `ws_float32_bits` / `rest_float32_bits` for audit.

Scope note: this refines only the WS-vs-REST *evidence comparison*. The production
Open=High / Open=Low strategies are unaffected — they compare open against high/low from the
**same** WS packet's float32 fields (identical representation), so no canonicalization is
involved on the trading path.

## R4 — Resumable multi-window collection
R4B also could not reliably capture all three required windows or CSOA16 continuity in one
straight-through run. R4C adds identity-level `pending_instruments` (expected identities with
no WS observation) and a `combine` step (`combine_records`) that merges per-window partial
records for one session into a single evaluable record. The combine:

- refuses to span sessions (differing `trading_date` / `session_identity` / `source_sha`);
- recomputes coverage, pending identities, and `sample_windows` from the union;
- keeps the earliest observed late-start (the true subscription-time capture); and
- for reconnect continuity spans the **earliest pre → latest post** across all observed
  reconnects, so a later continuity loss cannot be masked by an earlier good capture.

## R5 — Governance
Enablement remains a governed composition change (ADR-009 CSOA20): this addendum changes no
authority bit and adds no operator flag. Acceptance still requires a live evidence record
that evaluates ACCEPTED with full identity coverage, all required windows, and observed
CSOA16 continuity — collected under a separate, explicitly authorized live session, not by
this offline correction.
