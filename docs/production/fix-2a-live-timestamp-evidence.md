# FIX-2A — Dhan live-timestamp diagnostic evidence (RC3 gate)

**Status:** DRAFT — read-only analysis complete; **live evidence pending the Tue 2026-09-15 NSE
session**. No production change. No FIX applied. `RC3_CONFIRMED = INCONCLUSIVE`,
`READY_FOR_FIX2_IMPLEMENTATION = NO`.

**Purpose:** confirm or reject the suspected Dhan Last-Traded-Time (LTT) timezone-interpretation
defect using existing FIX-1 production diagnostics during a live NSE session. This phase does **not**
implement FIX-2.

## Execution context (why live evidence is not yet captured)

- **Date of this analysis:** Sunday **2026-09-13** (~17:57 IST). NSE cash market **closed**; the next
  regular session is **Tue 2026-09-15, 09:15 IST** — ~2 days ahead. Per §1, closed-market evidence
  alone must not confirm RC3.
- **Production inspection unavailable from this environment:** a read-only SSH probe to the authorized
  host `65.2.105.7` returned `Permission denied (publickey)` — no key is provisioned here. Host-key
  verification was **not** weakened (that boundary is respected). Consequently the §6 baseline, §7
  diagnostic-availability, §13 server-clock check, and §8–§10 live snapshots **could not be
  collected**. They must be gathered during the Sep 15 window from an environment with provisioned
  read-only access.
- **Baseline (recorded, NOT re-verified this phase):** production `main` SHA
  `a6b8c68ddd87e5d2a00c19485a2bf116285641fe`; image digest
  `sha256:42e99cc5bd943a826569f38ca57546cf236d968b9381484301133a8eba4a4378` (from prior records — not
  re-read live). Safety posture (strategies/session-authority/trading OFF; provider ON; RC 17; IPC
  OFF) is the *expected* state but was **NOT verified** this phase → `PRODUCTION_BASELINE = UNVERIFIED`
  (not a mismatch; simply not readable). The Sep 15 window must verify it first (§6).

## Read-only code trace (repository, `main` a6b8c68)

Decode → canonical event → validation:

| Step | Location | Behaviour |
|------|----------|-----------|
| LTT field | `adapters/dhan/live.py:41-42` (`_QUOTE_PAYLOAD`/`_FULL_PAYLOAD` `struct` fmt) | `last_trade_time` is a signed **int32** in the binary payload. |
| epoch → datetime | `adapters/dhan/live.py:551-554` `_epoch_timestamp(value)` | `return datetime.fromtimestamp(value, tz=UTC)` — interprets the LTT integer as a **POSIX epoch** and yields a UTC-displayed **aware** datetime. |
| event stamp | `live.py:358,408,432,440,448` | `event_timestamp = _epoch_timestamp(last_trade_time)` for tick/quote/depth. |
| future-time reject | `market_engine/validation.py:23,70` | `_MAX_FUTURE_SKEW = timedelta(minutes=1)`; `if event.event_timestamp > now + max_future_skew: return INVALID`. |
| diagnostic | `market_engine/tick_diagnostics.py:33,51,120` | `last_rejected_event_clock_delta_seconds = event_timestamp - now` at the reject decision — the FIX-1 metric. |

So a live tick whose `event_timestamp` is materially in the future (relative to the injected UTC
`now`) is rejected `INVALID`, and the recorded delta is `event_timestamp - now`. The future-reject
guard itself is correct (it must keep rejecting genuinely-future events — FIX-2B test T5); any defect
is in the LTT **interpretation**, not the validator.

## Dhan LTT timestamp semantics (§16 / §18)

`DHAN_LTT_TIME_SEMANTICS = UNKNOWN (official) — official docs state EPOCH, timezone NOT stated.`

- **VERIFIED_OFFICIAL** (dhanhq.co/docs/v2/live-market-feed, accessed 2026-09-13): LTT is an int32
  field labelled "EPOCH"; timezone is **not stated**.
- **VERIFIED_SDK** (DhanHQ-py `convert_to_date_time`, `src/dhanhq/dhanhq.py`): `IST =
  timezone(timedelta(hours=5,minutes=30)); dt = datetime.fromtimestamp(epoch, IST)`.

### Load-bearing subtlety (why the naive +5:30 hypothesis is NOT yet proven)

`datetime.fromtimestamp(x, tz)` treats `x` as an **absolute POSIX instant** (seconds since the UTC
epoch); the `tz` argument changes only the **display**, not the instant. Therefore the SDK's
`fromtimestamp(value, IST)` and ApexScan's `fromtimestamp(value, UTC)` compute the **same absolute
instant** from the same integer — `fromtimestamp(v, UTC) == fromtimestamp(v, IST)` as aware
datetimes. Two consequences:

1. The **UTC-vs-IST choice in the consumer does not, by itself, create a +5:30 instant error.** A
   +5:30 future shift can only arise if the raw LTT integer is *not* a true POSIX epoch (i.e. it is an
   IST wall-clock value stuffed into an epoch field, ≈ `true_utc_epoch + 19800`).
2. If the raw value were IST-naive (+19800), the SDK's `fromtimestamp(value, IST)` absolute instant
   would *also* be +19800 off — so the SDK's design (`fromtimestamp(epoch, IST)`) actually implies
   Dhan sends a **true POSIX epoch** displayed in IST, under which **ApexScan's `fromtimestamp(value,
   UTC)` is already the correct instant** and there would be no +5:30 error.

**This means the original "LTT interpreted as UTC instead of IST → +5:30 future" hypothesis is in
tension with the reference-SDK semantics.** It is not disproven — the FIX-1 production delta is real
observed data — but it cannot be confirmed from code/docs alone, and a naive `timestamp -= 5:30`
would be wrong if Dhan in fact sends a true epoch. (Caveat: the SDK evidence is from the REST/
historical `convert_to_date_time` utility; the SDK's *marketfeed* LTT path was not separately
confirmed and may differ — another reason to trace the raw value live.)

### The decisive live test (§14)

Capture, for a **known** live trade during the Sep 15 session, the **raw LTT integer** and compute
`fromtimestamp(raw, UTC)`; compare to the true UTC instant of that trade (≈ receive time):

- If `fromtimestamp(raw, UTC) ≈ true instant` (delta ≈ 0) → Dhan sends a **true POSIX epoch**;
  ApexScan is correct; the +delta in FIX-1 has another cause (§12) → **RC3 likely NO**.
- If `fromtimestamp(raw, UTC) ≈ true instant + 19800` → Dhan sends an **IST-naive epoch**; the fix is
  to interpret the decoded value as **IST wall-clock and localize** (not a blind subtraction) →
  **RC3 = YES** (Hypothesis B, §16).

## Alternative causes to rule out before confirming (§12)

Host/container clock wrong (§13 — unverified, production inaccessible); stale previous-session LTT
(the closed-market +15310 is consistent with staleness *plus* a possible shift, so it cannot isolate
the tz error — only live data can); ms-vs-s unit error; decoder byte-offset / wrong field read as
LTT; framing regression (framing fix `2deddf1` is in `main`; confirm no framing errors in the live
snapshots — do not modify framing); session-calendar error.

## Suspected root-cause candidate (§15 — NO CHANGE MADE)

- `FILE` = `backend/app/adapters/dhan/live.py`
- `SYMBOL` = `_epoch_timestamp` (line 551), feeding `event_timestamp` in `_decode_quote_packet` /
  `_decode_full_packet`.
- `CURRENT_BEHAVIOR` = treats the LTT int32 as a POSIX epoch → `datetime.fromtimestamp(value, UTC)`.
- `EXPECTED_BEHAVIOR` = interpret LTT per Dhan's **actual** semantics as confirmed by the live
  raw-value trace: if IST-naive, localize the value as IST wall-clock then convert to UTC (proper
  tz handling, **never** a hardcoded `-5:30`, §17); if a true epoch, leave as-is and investigate the
  other §12 causes. **Confidence: LOW-to-MEDIUM** — candidate localized, hypothesis not confirmed.

## Sep 15 live-window collection plan (what to gather)

Verify market open + real ticks flowing + current-session data (§2). Take snapshots A/B/C without
restarting anything, recording per §9: `timestamp_ist, production_sha, ticks_received, ticks_accepted,
total_rejected, rejected_invalid, other_reasons, clock_delta_seconds (min/max/typical),
provider_connected, reconnect_count`, plus (where safely observable) the **raw LTT integer**, the
decoded event datetime, and the receive datetime for ≥1 representative instrument. Also do the §13
host/container clock sanity read. Then apply the §10 confirmation criteria and the decisive live test
above.

## Decision

- `RC3_CONFIRMED = INCONCLUSIVE` — no live current-session evidence collected (market closed +
  production inaccessible from this environment); and code/SDK analysis raises a genuine tension with
  the +5:30-interpretation hypothesis that only a live raw-value trace can resolve.
- `READY_FOR_FIX2_IMPLEMENTATION = NO`.
- Do **not** implement FIX-2. Do **not** hardcode a `-5:30` offset.

## Decoupling consistency note (§23, do not act here)

A future FIX-2 must be applied at the canonical event-construction boundary so the **same** timestamp
semantics hold for both the legacy backend live path and the future `market-ingestion` canonical
path — avoid two timestamp semantics. The decoupling/holiday branch is **not** modified by this
phase.

## Provisional FIX-2B test matrix (only if RC3 later confirms — §22)

T1 documented Dhan LTT converts correctly · T2 canonical timestamp tz-aware · T3 live-equivalent
event no longer +5:30 future · T4 valid current event accepted · **T5 genuinely-future event still
rejected** · T6 stale behaviour unchanged · T7 IST-midnight boundary · T8 UTC-midnight does not
mis-set trading date · T9 09:15 open · T10 close/session boundaries · T11 stacked-frame parsing
unaffected · T12 RequestCode-17 quote decode unaffected · T13 historical/non-live paths unaffected ·
T14 weekend/holiday classification unaffected.
