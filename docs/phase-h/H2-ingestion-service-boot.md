# Phase-H2 — Market-Ingestion Service Boot + Provider Lifecycle (IPC OFF)

Governed by **ADR-025**. H2 turns the inert H1 skeleton into a real, independently-bootable
market-ingestion service that owns the Dhan auth/provider/WebSocket lifecycle — **with IPC
publication still OFF**.

> **H2 DOES NOT PUBLISH IPC. H2 DOES NOT FEED THE TICKENGINE. H2 DOES NOT CHANGE AUTHORITY.
> H2 DOES NOT DEPLOY PRODUCTION.** Live boot is exercised only against fakes; real Dhan is not
> contacted.

## What H2 adds

- **`app/market_ingestion/service.py`** — `MarketIngestionService` now boots a real provider
  lifecycle when enabled: it constructs a `ProviderCoordinator` over the injected provider
  (`connect` → healthy probe), starts a `ProviderSupervisor` on the live subscription, and reaches
  `RUNNING`. On any startup failure it cleans up, records `FAILED`, and re-raises (never a false
  `RUNNING`/`READY`). `stop()` cancels the supervisor and disconnects the provider deterministically
  (idempotent). Statuses: `DISABLED`/`NOT_STARTED`/`STARTING`/`RUNNING`/`FAILED`/`STOPPING`/`STOPPED`.
- **`app/market_ingestion/supervisor.py`** — `ProviderSupervisor` iterates `stream_market_data`
  into the sink and self-heals a terminal stream failure with bounded backoff (provider-only; the
  adapter still owns within-stream reconnect). Cancellation propagates cleanly.
- **`app/market_ingestion/sink.py`** — `ProviderOnlyEventSink`, a non-authoritative bounded counter
  (no TickEngine, no Redis, no IPC) — the explicit destination for decoded events while the
  publisher is off.
- **`app/market_ingestion/composition.py`** — `compose_market_ingestion_service(settings)`: lazy
  provider construction (Dhan adapter imported only inside the enabled branch), universe resolution,
  and `SubscriptionRequest` build. Disabled → inert service.
- **`__main__.py`** — the entrypoint now composes, starts, and (when enabled) serves until the
  supervisor ends, then shuts down; disabled remains a clean inert exit.
- **`Settings`** — a fail-fast **single-Dhan-owner** guard: `MARKET_PROVIDER_ENABLED` (backend
  legacy provider) and `MARKET_INGESTION_SERVICE_ENABLED` may not both be true in one config
  (ADR-025 §B6).

## Ownership after H2

The ingestion service, when enabled, owns one Dhan auth manager / provider / WebSocket, the live
subscription lifecycle, provider reconnect (above the adapter's own), and canonical decoding. The
backend keeps the TickEngine, APIs, and — under the default `LEGACY_ONLY` mode — the legacy Dhan
path. The ingestion service never imports the backend TickEngine as authority.

## Key decisions

- **M1 epoch is NOT allocated** (ADR-025 decision A): producer identity is meaningful only for
  canonical IPC publication, which is OFF in H2. No epoch is consumed by a provider-only boot.
- **No Redis** in provider-only boot: the publisher is off, so there is no XADD, no reference HASH
  write, no dedup key, and no Redis connectivity requirement.
- **Auth ownership**: one service lifecycle ⇒ one `DhanAuthManager` instance. `connect` is
  idempotent and token generation is lazy/cached, so a stream reconnect re-iterates the stream on
  the **same** provider/auth owner — it never rebuilds the auth manager or regenerates the token.
- **Config shape**: `MARKET_INGESTION_SERVICE_ENABLED=true` with `IPC_PUBLISHER_ENABLED=false`
  derives `MarketPathMode.LEGACY_ONLY` (the service runs but the market path is still legacy). This
  is representable in the ADR-025 matrix — no new mode, no deviation.

## What H2 explicitly does NOT do

No IPC publish/consume, no M1 epoch allocation, no M2 boundary/worker, no D1 publisher, no Redis,
no CompositeDeduplicator (C1), no L1 as live publication authority, no shadow/dual/cutover, no
TickEngine/timestamp/FIX-2 change, no production contact, no real Dhan connection. M1/D1/M2/C1/L1
semantics are unchanged. Shadow publication is **H3** (requires the B6 operator decision and
explicit authorization).

## Verification

Focused H2 tests prove: disabled inertness; enabled fake-provider boot → RUNNING with events to the
sink; reconnect on the same provider/auth owner; failed-start cleanup (FAILED, provider
disconnected); deterministic idempotent shutdown; and spied zero calls to M1/M2/D1/Redis. Full
backend suite green; import purity preserved. `docker compose config` was **not executed** (no
Docker locally); compose profile-gating validated structurally (default stack excludes
`market-ingestion`, no public port).
