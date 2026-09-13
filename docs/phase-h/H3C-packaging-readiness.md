# PHASE H3C — Shadow-publish packaging & long-lived process readiness

**Status:** delivered via PR into the `feature/decoupling-hardening` holiday branch (NOT `main`).
**Governing architecture:** ADR-026 (no new ADR — H3C hardens the frozen H3 design for long-lived
execution; it does not redesign it and introduces no second trading-calendar authority).

H3A composed the shadow-publish pipeline and H3B hardened its failure/recovery paths. **H3C makes
that same pipeline safe to run as a long-lived process**: it closes the Redis client lifecycle leak,
replaces the static trading date with a per-event exchange-local session date, adds graceful
signal-driven shutdown, and validates the Compose packaging. It does **not** live-activate H3, does
**not** connect real Dhan, does **not** enable the IPC consumer/C1, and does **not** change the
backend TickEngine/authority. `B6_DECISION = NO_LIVE_H3_YET` is unchanged.

## H3A/H3B baseline (as read from the code, before → after H3C)

| Fact | Before H3C | After H3C |
|------|------------|-----------|
| `REDIS_CLIENT_OWNER` | `composition._build_publication` builds `Redis.from_url(...)`, embedded in the D1 stream/atomic publisher; the frozen `PublicationStack` did **not** hold the handle | `PublicationStack.redis` holds the one client; the ingestion service is the single lifecycle owner |
| `REDIS_CLIENT_CLOSE_PATH` | **NONE** — never closed (NIT-6, process-lifetime leak) | `MarketIngestionService._close_publication()` → `PublicationStack.aclose()`, called from `stop()` and `_cleanup_after_failed_start()` |
| `TRADING_DATE_PROVIDER` | `_StaticTradingDate` — a fixed date captured at compose time (`now.date()`) (NIT-8) | `SessionTradingDate` — the exchange-local session date from the canonical `MarketSessionClassifier` |
| `TRADING_DATE_REFRESH_MODEL` | value fixed for the process lifetime | evaluated **per event** at `publisher.prepare()` (the publisher already called `current_trading_date()` per event; only the value was static) |
| `REFERENCE_KEY_DATE_SOURCE` | `md:reference:<envelope.trading_date>`, stamped at `prepare()` | unchanged plumbing; the date is now the live session date |
| `MAIN_PROCESS_SHUTDOWN_PATH` | `wait()` → `stop()`; no signal handling (a cancelled process skipped `stop()`) | `_wait_for_shutdown()` races the supervisor end vs SIGTERM/SIGINT; `stop()` runs in a `finally` (drain + close on any exit) |
| `COMPOSE_COMMAND` | `["python", "-m", "app.market_ingestion"]` | unchanged |
| `COMPOSE_PROFILE` | `["market-ingestion"]`; `restart: "no"`; depends on Redis only; no Postgres; no public port | unchanged (validated by test + CI `docker compose config`) |

## Redis client lifecycle

The composition root creates exactly one Redis client per incarnation and hands it to the
`PublicationStack`, which owns it. The service closes it as the **final** shutdown step, and only
after M2 has drained and L1 is finalised (the worker publishes through this client, so it must not
close first — §5). Ownership is single: one creator (composition), one holder (the stack), one
closer (the service).

Close is triggered on every terminal path and is exactly-once (guarded by
`_publication_closed`):

* **clean shutdown** — `stop()`: stop intake → `boundary.stop()` (bounded drain) →
  `continuity.drain_completed` → `aclose()`.
* **terminal publication failure** — `_fail_closed()` marks FAILED (no drain there); the entrypoint's
  `finally` calls `stop()`, which drains then closes.
* **provider/M2 startup failure** — `_cleanup_after_failed_start()` drains the boundary then closes
  the client immediately (bulletproof even if the caller never calls `stop()`).
* **top-level cancellation (SIGTERM/SIGINT)** — the entrypoint's `finally: await service.stop()`
  drains and closes.

`redis-py`'s `aclose` is itself safe to call more than once; the service still guarantees a single
close so a spy sees exactly one.

## Dynamic trading date

`SessionTradingDate.current_trading_date()` returns
`MarketSessionClassifier.classify(now()).trading_date`. `MarketSessionClassifier` is the canonical
market-IPC trading-date authority (see `app.market_ipc.consumer`), reused rather than reinvented —
H3C introduces **no** second trading-calendar authority (no `H3C_ARCHITECTURE_DECISION_REQUIRED`).

The trading date is **calendar-independent**: `classify().trading_date` is the exchange-local date of
the instant (the calendar/coverage only affects `market_state`). So the settings-derived classifier
yields the identical trading date without resolving the full calendar dataset — that heavier,
fail-closed dataset resolution belongs to live activation (H3D+), not to packaging. The classifier
converts to the exchange timezone **before** taking the date, so:

* a long-lived producer crosses an exchange-local midnight (D1 → D2) with **no restart and no new
  epoch** — the reference key `md:reference:<date>` follows the live session date automatically;
* a **server/UTC** wall-clock midnight never shifts the trading date (only an exchange-local one
  does);
* on a weekend/holiday the source returns that day's canonical exchange-local date (never a stale or
  wall-clock date); in practice no events arrive on a non-trading day, so no reference is written.

The publisher already read `current_trading_date()` per event, so no publisher/D1 change was needed.

## Process shutdown & signals

`python -m app.market_ingestion` installs SIGTERM/SIGINT handlers (best-effort — skipped on a
platform/thread without asyncio signal support) and serves until the supervisor ends **or** a signal
arrives. Either way `stop()` runs in a `finally`, so `docker stop` (SIGTERM) drains M2, finalises L1,
and closes Redis instead of being killed mid-publication. No custom signal framework is added — this
is the standard asyncio graceful-shutdown pattern, and the standalone producer has no uvicorn
lifecycle to inherit one from.

### Exit codes

| Situation | Exit |
|-----------|------|
| disabled (default) | 0 |
| clean operator shutdown / clean stream end | 0 |
| compose/startup configuration failure | 1 |
| terminal publication failure (`terminal_failure`) | 1 |
| provider terminal failure surfaced as a terminal break | 1 |

Bounded reconnect exhaustion is a **test-only** finite `max_reconnects` configuration and
intentionally ends the supervisor cleanly (exit 0); production uses `max_reconnects=None` (unbounded),
so this never applies in a deployed process (H3B LOW-1, unchanged — not redesigned).

## Compose packaging

`docker-compose.yml`'s `market-ingestion` service is validated (structurally by
`tests/deploy/test_compose_dev.py`, and by `docker compose config` in the Docker-capable CI
`infrastructure` job):

| Assertion | Value |
|-----------|-------|
| `DEFAULT_INGESTION_STARTED` | **NO** — gated behind `profiles: ["market-ingestion"]` |
| `PROFILE_INGESTION_INCLUDED` | **YES** — present under `--profile market-ingestion` |
| `PUBLIC_PORTS` | **NONE** — no `ports` (producer-only, ADR-026 §20) |
| `POSTGRES_DEPENDENCY` | **NONE** |
| `REDIS_DEPENDENCY` | **YES** — `depends_on: redis (service_healthy)` |
| command / restart | `["python","-m","app.market_ingestion"]` / `restart: "no"` |
| singleton | daemon-global unique `container_name: apexscan-market-ingestion` (ADR-026) |

## Health / readiness

No public health port is added (§20). Readiness is derivable from the existing bounded
`ServiceDiagnostics`: **alive** = `status is RUNNING`; **publication-ready** = additionally
`provider_connected` and `continuity_state != "broken"` (provider connected/subscribed, M2 running,
L1 healthy, no terminal break). This is the existing H3 contract — H3C confirms it rather than adding
a new surface.

## Long-lived guarantees (proven with a fake provider + real test Redis)

* trading-date rollover D1 → D2 within one running incarnation (no restart, epoch stable);
* reconnect across a rollover preserves the epoch and the coordinator/auth (one `connect`);
* a 60-cycle reconnect soak keeps one epoch, one worker, one client, no terminal state;
* a 2000-event episode stays within the bounded queue (no overflow) and publishes everything;
* Redis closes exactly once, after the drain, on clean/terminal/failed-start/cancelled paths.

## Explicitly NOT in H3C

No live activation, no real Dhan/WS/token, no production contact/deploy, no IPC consumer/C1, no
shadow compare, no authoritative sink, no H4, no cutover, no TickEngine/authority change, no
FIX-2/timestamp change, no D1 Lua/M1/M2/L1 semantic change, no second trading-calendar authority, no
change to `docker-compose.production.yml`. `B6_DECISION = NO_LIVE_H3_YET`; B4/B10/B11 unchanged.
`READY_FOR_H3D = YES`, `READY_FOR_H3_LIVE_ACTIVATION = NO`, `READY_FOR_H4 = NO`,
`READY_FOR_IPC_AUTHORITY = NO`.
