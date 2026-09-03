# Previous-Close Provenance (SECTOR-VIEW-1A)

This documents where the live `MarketContext.previous_close` comes from, what it
is guaranteed to be, and what it is deliberately *not*. It closes the
SECTOR-VIEW-1 blocker: Dhan's live previous-close packet was decoded then
discarded, so previous close was unreachable on the live context.

See ADR-017 for the decision record.

## Flow

```
Dhan WebSocket (response code 6, previous-close packet)
  → _decode_previous_close_packet(packet, reference)        [app/adapters/dhan/live.py]
      emits MarketReference(instrument, previous_close>0), or () when value ≤ 0
  → market_runtime._dispatch                                [app/services/market_runtime.py]
      routes MarketReference to the engine (alongside Tick/Quote)
  → TickEngine._accept_reference                            [app/market_engine/tick_engine.py]
      stamps engine clock time, classifies session, sets previous_close
  → MarketContext.previous_close: Decimal | None            [app/market_engine/context.py]
      read by any generic consumer (e.g. SECTOR-3 ConstituentObservation)
```

## Guarantees

- **Provider-independent.** Consumers read `MarketContext.previous_close`; they
  never see a Dhan type. The canonical carrier is `MarketReference`, which has
  no broker naming.
- **Never fabricated.** A missing or zero provider value (Dhan's absent
  sentinel) produces no `MarketReference`; `previous_close` stays `None`.
  Strictly positive when present (`> 0`).
- **Preserved across ticks.** Tick/Quote events never carry previous close.
  Because `with_update` replaces fields, the engine carries a known value
  forward explicitly on every accepted update.
- **Reset on rollover.** A trading-date change (detected from the accepted
  event's classified session) clears previous close; the new session's value
  arrives via its own `MarketReference`. No cross-day leak.
- **Fails closed.** A `MarketReference` for an unknown instrument is rejected
  (`INVALID`), with no state mutation and no lifecycle publication.
- **Per-instrument isolation.** A reference for one instrument never affects
  another (independent `MarketContext` per instrument).

## What this is NOT

- **Not `session_open`.** `session_open` (raw WS `tick.session_ohlc.open_price`)
  is unchanged. Previous close is the *prior* session's close, a separate fact.
- **Not authoritative session statistics.** This does not route through, enable,
  or alter the SessionStatisticsAuthority / ADR-009 enable path (still gated
  off in this slice).
- **Not historical/REST/DB derived.** The only source is the live WS packet.
  There is no fallback.
- **Not a timed market event.** The provider packet carries no timestamp; the
  engine stamps its own clock time on accept. `MarketReference` is therefore not
  an `_EventData` subtype.

## Live caveat

Live subscriptions request `TICK` mode. Whether Dhan delivers code-6 packets in
that mode cannot be verified offline. If it does not, `previous_close` remains
`None` and consumers degrade exactly as they do today — no behaviour regression.

## Verified by

- `tests/unit/market_engine/test_tick_engine_previous_close.py` — decode-to-set,
  missing→None, carry-across-ticks/quote, later-reference-replaces, unknown→
  INVALID, per-instrument isolation, trading-date rollover reset, new-day supply.
- `tests/unit/test_dhan_live.py` — packet→`MarketReference`; zero value emits
  nothing.
- `tests/unit/test_market_data_contracts.py` — `MarketReference` immutable,
  broker-neutral, strictly positive, in the `MarketData` union.
- `tests/unit/market_engine/test_market_context.py` — `previous_close` default
  `None`, `with_update` set/clear, must be positive.
- `tests/architecture/test_import_boundary.py` — the Market Engine imports no
  Dhan or sector code.
