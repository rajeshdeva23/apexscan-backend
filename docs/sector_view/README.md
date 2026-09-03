# Sector View — Live Shadow Runtime (SECTOR-VIEW-1B)

A **passive, read-only** live Sector Intelligence shadow runtime over the existing Dhan feed
and Market Engine. It subscribes to generic `MarketContext` events, maintains bounded
latest-observation state, and on a periodic cadence reuses the SECTOR-2/3/4 engines to produce
an internal `SectorShadowSnapshot`. It changes no trading, strategy, provider, or engine
behavior and exposes no public API.

**Disabled by default.** Nothing runs unless `settings.sector_shadow_enabled=true`.

## Status

| Item | State |
|------|-------|
| Canonical `previous_close` support (VIEW-1A) | **PASS** — merged (`MarketReference` → `MarketContext.previous_close`) |
| Passive shadow runtime (VIEW-1B) | Implemented offline; disabled by default; not deployed |
| **Live Dhan code-6 delivery under current subscription mode** | **UNVERIFIED** — see below |

> ⚠️ **UNVERIFIED live delivery.** VIEW-1A made previous_close a first-class canonical field,
> but it did **not** prove that Dhan actually delivers the previous-close packet (response
> code 6) under the currently configured `TICK` subscription mode. Until controlled live
> evidence confirms delivery, `previous_close` may be `None` for some or all instruments in
> production. The shadow runtime is built to stay healthy in all three cases (all / some / none
> available) and never fabricates a value. Do not read "canonical support exists" as "delivery
> is proven."

## Documents

- [PREVIOUS_CLOSE_PROVENANCE.md](PREVIOUS_CLOSE_PROVENANCE.md) — where `previous_close` comes
  from (VIEW-1A) and what it is / is not.
- [LIVE_SHADOW_RUNTIME.md](LIVE_SHADOW_RUNTIME.md) — architecture, flow, and the input
  provenance for every field.
- [SHADOW_SNAPSHOT_CONTRACT.md](SHADOW_SNAPSHOT_CONTRACT.md) — the internal snapshot fields and
  their meaning.
- [RUNTIME_SAFETY.md](RUNTIME_SAFETY.md) — isolation, boundedness, lifecycle, and failure
  behavior guarantees.

Related: ADR-016 (sector intelligence layer, Proposed), ADR-017 (canonical previous close,
Proposed), ADR-018 (passive shadow runtime, Proposed).

## Not done (out of scope for VIEW-1B)

No deployment, no enabling in production, no live validation, no calibration/scoring/labels, no
frontend, no REST/WS API, no persistence, no change to Dhan subscription mode, no SECTOR-5B.
