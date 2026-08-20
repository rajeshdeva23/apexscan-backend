# ADR-011 Evidence Record — NSE 2026 Capital-Market Trading Calendar

| Field | Value |
|-------|-------|
| **Type** | Subordinate immutable evidence record (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window |
| **Status** | Accepted evidence (date-level 2026 calendar authority) |
| **Date** | 2026-08-16 |
| **Phase** | ADR-011-DATA-EVIDENCE-R5 |
| **Scope** | NSE **Capital Market** segment (NSE_EQ cash equity, ADR-004). Evidence only — no production dataset/loader/wiring (that is ADR-011-DATA-R1-R4). |

This record is append-only. Do not rewrite prior entries.

---

## Evidence provenance

| Source | Circular date | How obtained this phase | Strength |
|--------|---------------|-------------------------|----------|
| **NSE/CMTR/71775** (Circular Ref 172/2025) — 2026 CM trading holidays | 2025-12-12 | **PRIMARY — opened verbatim** (archives.nseindia.com PDF, both pages; author "Khushal Shah, AVP"; segment header "CAPITAL MARKET SEGMENT") | **L1 (primary, this phase)** |
| **NSE/CMTR/72260** — adds 2026-01-15 as a CM holiday | 2026-01-12 | Re-fetch **timed out**; used at recorded strength from the accepted R2 evidence | **L1-recorded (R2 verbatim; not re-opened this phase)** |
| **NSE/CMTR/72349** — Live Trading Session 2026-02-01 | 2026-01-16 | Re-fetch **timed out**; used at recorded strength from the accepted R2 evidence | **L1-recorded (R2 verbatim; not re-opened this phase)** |

Governance note: per the standing evidence rule, previously-accepted attributable evidence is
**not downgraded merely because a URL cannot currently be re-fetched**; equally it is not
strengthened. CMTR/72260 and CMTR/72349 remain at recorded strength; CMTR/71775 is upgraded to
primary (its enumerated list — the R4 completeness blocker — is now verbatim).

## CMTR/71775 — verbatim weekday holiday table (page 1)

| # | Date | Day | Holiday |
|---|------|-----|---------|
| 1 | 2026-01-26 | Monday | Republic Day |
| 2 | 2026-03-03 | Tuesday | Holi |
| 3 | 2026-03-26 | Thursday | Shri Ram Navami |
| 4 | 2026-03-31 | Tuesday | Shri Mahavir Jayanti |
| 5 | 2026-04-03 | Friday | Good Friday |
| 6 | 2026-04-14 | Tuesday | Dr. Baba Saheb Ambedkar Jayanti |
| 7 | 2026-05-01 | Friday | Maharashtra Day |
| 8 | 2026-05-28 | Thursday | Bakri Id |
| 9 | 2026-06-26 | Friday | Muharram |
| 10 | 2026-09-14 | Monday | Ganesh Chaturthi |
| 11 | 2026-10-02 | Friday | Mahatma Gandhi Jayanti |
| 12 | 2026-10-20 | Tuesday | Dussehra |
| 13 | 2026-11-10 | Tuesday | Diwali-Balipratipada |
| 14 | 2026-11-24 | Tuesday | Prakash Gurpurb Sri Guru Nanak Dev |
| 15 | 2026-12-25 | Friday | Christmas |

## CMTR/71775 — holidays falling on Saturday/Sunday (page 2)

| # | Date | Day | Description | Classification |
|---|------|-----|-------------|----------------|
| 1 | 2026-02-15 | Sunday | Mahashivratri | Category B — already closed by weekend default (informational; not an added closure) |
| 2 | 2026-03-21 | Saturday | Id-Ul-Fitr (Ramadan Eid) | Category B — weekend default |
| 3 | 2026-08-15 | Saturday | Independence Day | Category B — weekend default |
| 4 | 2026-11-08 | Sunday | Diwali Laxmi Pujan* | **Category C — exceptional OPEN (Muhurat)** |

\* Verbatim footnote: *"Muhurat Trading will be conducted on Sunday, November 08, 2026. Timings
of Muhurat Trading shall be notified subsequently."* ⇒ **date-level OPEN = PROVEN; intraday
timing = NOT_PROVEN** (H3 fail-closed for intraday history touching this date).

## Amendment — CMTR/72260 (recorded)
Adds **2026-01-15 (Thursday) — CM trading holiday** (Maharashtra Municipal Corporation
elections). Applied as an additional weekday closure. Recorded strength.

## Special OPEN — CMTR/72349 (recorded)
**2026-02-01 (Sunday)** — Live Trading Session (Union Budget), Capital Market. Normal Market
**one continuous block 09:15–15:30 IST** (recorded strength). No multiple normal-market
intervals recorded; pre-open/block-deal/post-close periods are excluded from the normal-market
interval per §3.

## Normalized 2026 sets (evidence level — NOT yet a production dataset)

**closed_dates** (weekday closures beyond the weekend default — 16):
```
2026-01-15 (amend CMTR/72260), 2026-01-26, 2026-03-03, 2026-03-26, 2026-03-31,
2026-04-03, 2026-04-14, 2026-05-01, 2026-05-28, 2026-06-26, 2026-09-14,
2026-10-02, 2026-10-20, 2026-11-10, 2026-11-24, 2026-12-25
```
(Category-B weekend-falling holidays 2026-02-15, 2026-03-21, 2026-08-15 are **not** in
`closed_dates` — the weekend rule already closes them; listed for completeness only.)

**open_sessions** (exceptional OPEN — 2):
```
2026-02-01 (Budget, CMTR/72349), 2026-11-08 (Muhurat, CMTR/71775)
```

**session_overrides**:
```
2026-02-01 -> ( TradingInterval(09:15, 15:30), )        # recorded, CMTR/72349
2026-11-08 -> (none — timing NOT_PROVEN; intraday fail-closed per H3)
```
No date is both open and closed (verified). Both OPEN dates are Sundays promoted to trading by
`open_sessions` precedence (M5).

## A–J completeness matrix

| Property | Source | Evidence | Verdict |
|----------|--------|----------|---------|
| A. base holiday list complete | CMTR/71775 | verbatim 15-row weekday table + 4-row weekend table | **PROVEN** |
| B. Jan-15 amendment incorporated | CMTR/72260 | recorded fact; applied to closed_dates | **PROVEN (recorded)** |
| C. weekday closures complete | A+B | 16 weekday closures normalized | **PROVEN** |
| D. weekend default semantics | governed model | Sat/Sun default-closed | **PROVEN** |
| E. exceptional OPEN dates represented | CMTR/72349, CMTR/71775 | 2026-02-01 + 2026-11-08 | **PROVEN** |
| F. 2026-02-01 timing complete | CMTR/72349 | 09:15–15:30 single block (recorded) | **PROVEN (recorded)** |
| G. Muhurat date-level OPEN | CMTR/71775 | page-2 footnote | **PROVEN (primary)** |
| H. Muhurat intraday timing | CMTR/71775 | "notified subsequently" | **NOT_PROVEN** (acceptable; H3 fail-closed) |
| I. Capital Market segment applicability | CMTR/71775 header | "Department: CAPITAL MARKET SEGMENT" | **PROVEN** |
| J. contiguous 2026 date-level completeness | A–G | complete date classification for 2026-01-01..2026-12-31 | **PROVEN** |

Per §6, J does **not** require a negative certificate proving no other circular exists; the
authoritative annual calendar + recorded amendment/special-session evidence is the governed
completeness standard.

## Residual (known, not a completeness failure)
NSE occasionally announces ad-hoc special sessions outside the annual calendar (e.g. the 2024
DR-Saturday sessions). None is evidenced for 2026 here. Detecting such an event is the job of
ongoing circular ingestion + the secondary calendar monitor (ADR-011 calendar-monitor
governance); it does not retroactively invalidate this date-level authority.

## Verdict
**DATA_EVIDENCE_GATE = PASS** for 2026 date-level Capital-Market calendar authority.
2026-02-01 intraday timing PROVEN (recorded); 2026 Muhurat intraday timing NOT_PROVEN (H3
fail-closed). Production dataset/loader/wiring remain deferred to ADR-011-DATA-R1-R4.
