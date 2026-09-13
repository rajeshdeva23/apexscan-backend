# PHASE H3B — Shadow-publish failure-path hardening

**Status:** delivered via PR #59 into the `feature/decoupling-hardening` holiday branch (NOT `main`).
**Governing architecture:** ADR-026 (no new ADR — H3B hardens the frozen H3 design, it does not
redesign it).

H3A composed and proved the *happy path* of the decoupled shadow-publish pipeline (fake/replay
provider → `PublishingEventSink` → M1 identity → M2 bounded async boundary → D1 atomic Redis
publication → L1 continuity). **H3B proves the failure and recovery behaviour** of that same
pipeline against the real M2/L1 components (only the D1 publisher and the provider are doubles) and
against a real disposable Redis (redislite).

H3B does **not** live-activate H3, does **not** connect real Dhan, does **not** enable the IPC
consumer/C1, and does **not** touch the backend TickEngine/authority. `B6_DECISION = NO_LIVE_H3_YET`
is unchanged.

## H3A baseline (behaviour under test, as read from the code)

| Fact | Where |
|------|-------|
| `H3A_TERMINAL_SIGNAL_PATH` | Two sources set `service._terminal`: (a) the supervisor task ending with an exception (`PublicationTerminalError`) via `_on_supervisor_done`; (b) the bounded observer seeing `continuity.state is BROKEN`. `_watch_terminal` awaits it → `_fail_closed`. |
| `H3A_PROVIDER_FAIL_CLOSED_PATH` | `_fail_closed`: idempotent (guards on FAILED/STOPPING/STOPPED) → status FAILED → cancel supervisor + observer → `coordinator.shutdown()` (disconnect). Never self-heals. Boundary drain/L1 finalize happen in `stop()`. |
| `H3A_M2_FAILURE_OBSERVATION` | `_run_observer` polls `boundary.diagnostics()` every `observer_interval` (default 0.5 s) → `continuity.observe_boundary(...)`; a `FAILED` worker or a `publish_failure_total`/`overflow_total` delta becomes a terminal break. Worker faults are only visible via this poll. |
| `H3A_SHUTDOWN_ORDER` | `stop()`: cancel supervisor (stop intake) → `coordinator.shutdown()` → `boundary.stop()` (bounded drain) → `continuity.drain_completed(result)` → cancel observer + watcher → STOPPED. |
| `H3A_UNKNOWN_OUTCOME_BEHAVIOR` | D1 `transmit` returns only `PUBLISHED \| FAILED_TRANSPORT`; `FAILED_TRANSPORT` conflates definite failure with an unknown outcome and maps to a terminal `publication_failed` (never a false success; sequence never advanced as published). |
| `H3A_RECONNECT_BEHAVIOR` | Provider transport drop is recoverable: `ProviderSupervisor` self-heals (same provider/auth/epoch); `on_disconnect`/`on_reconnect` feed L1. L1 needs a post-reconnect successful publish to return HEALTHY (a reconnect alone is `AWAITING_RECOVERY_EVIDENCE`). |
| `H3A_REDIS_CLIENT_LIFETIME` | `composition.py` builds `Redis.from_url(...)` at process lifetime and does not close it on shutdown (NIT-6, deferred to H3C — matters only once `__main__` publisher mode runs live). |

## Failure matrix

Recoverable = handled within the incarnation (self-heal / continue). Terminal = fail closed for the
incarnation (stop intake, disconnect provider, mark FAILED); recovery requires a **new incarnation**
(process restart → new epoch → fresh L1).

| ID | Scenario | Service | L1 continuity | Provider | M2 / worker | Epoch | Class |
|----|----------|---------|---------------|----------|-------------|-------|-------|
| F1 | Redis down at startup (`ensure_group` fails) | FAILED (start raises) | NOT_STARTED | never connected | NOT_STARTED | **consumed** (file bumped before Redis check) | terminal-at-start |
| F2 | Redis disconnect during publication | FAILED | BROKEN `publication_failed` | disconnected | worker RUNNING, `publish_failures++` | unchanged | terminal |
| F3 | Definite D1 failure (`FAILED_TRANSPORT`) | FAILED | BROKEN `publication_failed` | disconnected | worker RUNNING, `publish_failures++` | unchanged | terminal |
| F4 | Unknown/ambiguous outcome | FAILED | BROKEN `publication_failed` (capability: `publication_outcome_uncertain`) | disconnected | as F3 | unchanged | terminal, no false success |
| F5 | M2 queue overflow | FAILED | BROKEN `publication_queue_overflow` | disconnected | queue full → explicit reject (no coalesce/overwrite) | unchanged (overflow burns a seq — legal gap) | terminal |
| F6 | M2 worker exception | FAILED | BROKEN `publication_worker_failed` | disconnected | `FAILED`, **no auto-restart** | unchanged | terminal |
| F7 | Provider disconnect with backlog | RUNNING | PROVIDER_DEGRADED | reconnecting | backlog keeps draining; accepted events **not** lost | stable | recoverable |
| F8 | Provider reconnect with backlog | RUNNING | `awaiting_recovery_evidence` → HEALTHY only after a publish | reconnected (same auth) | same worker | stable | recoverable |
| F9 | Sink raises `PublicationTerminalError` | FAILED | BROKEN | disconnected | — | unchanged | terminal (bypasses reconnect) |
| F10 | Cancellation during publication | STOPPED | STOPPED | disconnected | worker cancel counts in-flight for drain | unchanged | cooperative |
| F11 | Shutdown, empty queue | STOPPED | STOPPED `clean_shutdown` | disconnected | drained | unchanged | clean |
| F12 | Shutdown, non-empty (drainable) | STOPPED | STOPPED `clean_shutdown` | disconnected | drained within timeout | unchanged | clean |
| F13 | Shutdown drain timeout | STOPPED | STOPPED `incomplete_drain`, `pending_at_stop > 0` | disconnected | pending surfaced (incl. in-flight) | unchanged | honest-incomplete |
| F14 | Shutdown racing terminal worker fault | STOPPED | STOPPED (terminal reason kept unless incomplete) | disconnected once | idempotent | unchanged | safe race |
| F15 | Reconnect racing terminal publisher fault | FAILED | BROKEN | not resurrected | — | unchanged | terminal wins |
| F16 | Start fails after epoch alloc, before provider | FAILED | HEALTHY/NOT_STARTED (no break) | never streamed | stopped | **consumed**, next incarnation +1 | terminal-at-start |
| F17 | Start fails after M2 start, before provider ready | FAILED | (last observed) | disconnected | stopped/drained, no task leak | consumed | terminal-at-start |

## Terminal vs recoverable — the contract

* **Recoverable** = a *provider transport* drop. `ProviderSupervisor` self-heals with bounded
  backoff on the **same** provider/auth/epoch. L1 goes `PROVIDER_DEGRADED`, and returns HEALTHY
  only after a successful publish observed *after* the reconnect — a reconnected socket alone is
  `awaiting_recovery_evidence`, never HEALTHY.
* **Terminal** = a *publication continuity* break: queue overflow, M2 worker fault, a definite/
  unknown D1 failure, or a Redis outage. `PublishingEventSink` raises `PublicationTerminalError`
  on a terminal submit outcome; the supervisor has a dedicated `except PublicationTerminalError:
  raise` **ahead of** its generic reconnect catch-all, so a terminal break propagates (stops
  intake) instead of being self-healed into a reconnect loop. The observer independently trips the
  terminal signal for a worker fault the submit seam cannot see.

A terminal break is **sticky for the incarnation**. There is no in-process self-heal: no automatic
worker restart, no new epoch allocated in the same process, no provider resurrection.

## Restart boundary & epoch behaviour

The durable M1 epoch is file-backed (`DurableEpochAllocator`), monotonic, and allocated once per
incarnation by `boundary.start()` → `publisher.start()` — **only in publisher mode**. It is:

* **consumed even by a start that later fails** (the file counter is bumped before the Redis
  `ensure_group` check and before the provider health check), and never rolled back;
* **stable across provider reconnects** (a transport reconnect re-iterates the same provider — the
  epoch is not reallocated);
* **never reused** — a new incarnation over the same durable state directory allocates a strictly
  higher epoch, and a new incarnation gets a fresh L1 tracker.

## Unknown-outcome semantics (at-least-once, never exactly-once)

D1's `transmit` contract can only report `PUBLISHED` or `FAILED_TRANSPORT`. `FAILED_TRANSPORT`
conflates a *definite* failure with an *unknown* outcome (the client cannot tell whether Redis
committed). H3B keeps this honest:

* it is **never** treated as a confirmed success (`published_total` is not incremented, the
  published position is not advanced);
* it is **not** fabricated into a confirmed loss of a specific event — it is a terminal continuity
  break, and the accepted position legitimately leads the published position;
* L1 also carries a distinct `publication_uncertain` / `PUBLICATION_OUTCOME_UNCERTAIN` capability
  (terminal, sticky) reserved for a future publisher contract that *can* distinguish the two.

Exactly-once is not claimed; at-least-once transport can append the same envelope twice under one
canonical `(producer_id, producer_epoch, producer_sequence)` identity, which a future C1 consumer
deduplicates. C1 is not exercised here.

## Shutdown races

`stop()` is idempotent (returns early once STOPPED/DISABLED) and `_fail_closed()` guards on the
lifecycle state, so a `stop()` racing an observer/supervisor-driven terminal break converges to a
single STOPPED outcome with a single provider disconnect and no unhandled `PublicationTerminalError`
and no leaked tasks. `wait()` deliberately suppresses `CancelledError`/`PublicationTerminalError`
from the supervisor task so a fail-closed cancellation cannot skip graceful shutdown at the
entrypoint.

## The one code change in H3B

Everything above is the *frozen* H3A behaviour, now covered by tests. The single code change is a
fail-closed fix surfaced by the matrix:

* `MarketIngestionService.terminal_failure` — a read-only property (`self._terminal.is_set()`) that
  survives `stop()`, so a caller can tell a terminal incarnation from a clean shutdown after the
  service has drained to STOPPED.
* `python -m app.market_ingestion` now returns a **non-zero exit code** when an incarnation ends on
  a terminal publication break (previously it drained to STOPPED and exited 0, conflating a terminal
  break with a clean shutdown). A clean serve-then-stop still exits 0.

## Explicitly NOT in H3B

No live activation, no real Dhan/WS/token, no production contact, no IPC consumer/C1, no shadow
compare, no authoritative sink, no H4, no cutover, no FIX-2/timestamp change, no D1 Lua change, no
change to M1/M2/L1 semantics. `B6_DECISION = NO_LIVE_H3_YET`; `READY_FOR_H3C = YES`,
`READY_FOR_H3_LIVE_ACTIVATION = NO`, `READY_FOR_H4 = NO`.
