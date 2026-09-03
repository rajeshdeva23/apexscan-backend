# ADR-017 — Canonical Session Reference (Previous Close)

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-03 |
| **Deciders** | Platform / Market Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Refined by** | — |
| **Related** | `docs/05_DATA_PROVIDER.md`, `docs/06_MARKET_ENGINE.md` (§6, §11-§12), ADR-003, ADR-004, ADR-005, ADR-008, ADR-009, ADR-016 |

---

## Context

The prior trading session's **previous close** is a required input for the
Sector Intelligence layer (SECTOR-3 `ConstituentObservation` requires
`previous_close > 0`, ADR-016). It is also a general-purpose session reference
useful to any downstream market consumer.

The Dhan standard live feed delivers previous close in an auxiliary packet
(response code 6). Before this decision the adapter decoded that packet,
validated it, and then **discarded** it (`_decode_previous_close_packet`
returned `()`) — exactly the pattern ADR-005 corrected for cumulative volume.
No canonical live contract carried previous close, so it was unreachable on the
live `MarketContext`.

`MarketContext` already carries other session references (`session`,
`session_statistics`), but none of them is previous close, and the authoritative
session-statistics path (ADR-008/ADR-009) is capability-gated **off** in this
slice and does not model previous close.

## Problem

Previous close must reach generic consumers **without**:

- making the generic market domain depend on a Dhan-specific packet type
  (broker-blindness, ADR-003/ADR-004);
- fabricating a value when the provider does not supply one;
- routing through, enabling, or altering the SessionStatisticsAuthority /
  ADR-009 enable path, or changing `session_open` semantics;
- introducing a second feed, auth path, or any historical/REST/DB fallback.

Two structural facts shape the design:

1. `MarketContext.with_update` **replaces** fields (a `None` argument clears
   them). Since Tick/Quote events never carry previous close, a naive update
   would erase a known previous close on the very next tick. The engine must
   therefore carry it forward explicitly.
2. The Dhan previous-close packet has **no wire timestamp**. It is a session
   reference, not a timed market *event*, so it cannot be modelled as a timed
   event with a provider timestamp.

## Decision

**1. A new canonical contract `MarketReference`** (`app/schemas/market_data.py`),
added to the `MarketData` union:

```
MarketReference(instrument: Instrument, previous_close: Decimal > 0)
```

It is provider-independent (no Dhan naming), frozen, `extra="forbid"`, and
strict. It is **not** an `_EventData` subtype: it carries no `event_timestamp`,
reflecting that the source packet has none. A missing/zero provider value
(Dhan's absent sentinel) is never fabricated — the adapter emits nothing.

**2. A new optional field `MarketContext.previous_close: Decimal | None`**
(default `None`, `> 0` when present). Consumers read it generically; they never
see a broker type.

**3. Engine handling in `TickEngine`.** A `MarketReference` is routed to
`_accept_reference`, which stamps the engine's own clock time (the packet has
none), classifies the session from that time, preserves all prior observable
state (tick, quote, candles, session statistics, historical), and sets
`previous_close`. Unknown instruments fail closed (`INVALID`, no mutation, no
publication). On each accepted Tick/Quote the engine carries the known
previous close forward (`_carried_previous_close`) and **resets** it to `None`
only on a genuine trading-date rollover (the new session's previous close then
arrives via its own `MarketReference`).

**4. Adapter change.** `_decode_previous_close_packet` emits a
`MarketReference` (previous close `> 0`) instead of discarding it; the live
runtime `_dispatch` routes `MarketReference` to the engine alongside Tick/Quote.

## Consequences

- Previous close is available on the live `MarketContext` for any consumer,
  broker-neutrally, with no fabrication and no second feed/auth.
- No change to `session_open`, session statistics, or the ADR-009 enable path.
- The generic market domain imports no Dhan or sector code (enforced by the
  existing Market-Engine import-boundary test).
- **Caveat (delivery mode).** Live subscription requests `TICK` mode
  (`_LIVE_DATA_TYPES`). Whether Dhan delivers code-6 packets in that mode is a
  live-only fact not verifiable offline; this decision does not change the
  subscription mode. If code-6 is not delivered in TICK mode, `previous_close`
  simply remains `None` and consumers degrade exactly as they do today.
- This ADR documents a contract/engine addition only; it does not build the
  Sector Shadow Runtime and does not deploy.

## Status note

Proposed. Accepted ADRs (008/009/016) are unchanged. This ADR neither enables
authority nor alters `session_open` semantics.
