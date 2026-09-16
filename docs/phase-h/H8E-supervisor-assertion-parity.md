# H8E — decoupled provider-supervisor failure/recovery assertion parity (offline)

**Status:** offline hardening, tests-first. Closes the coverage asymmetry the post-H9A provider
failure/recovery audit found between the legacy in-process runtime and the **go-forward decoupled**
`MarketIngestionService` + `ProviderSupervisor` path. One minimal production fix (an idempotent
`start()` guard) was required, exposed by a failing test. No production/Dhan contact; no IPC
authority; no cross-process ownership wiring; no H9B work.

## Baseline
- Base: `feature/decoupling-hardening` `28d9eac` (H9A). Branch:
  `feature/phase-h8e-decoupled-supervisor-assertion-parity`.
- Production `main` `a6b8c68` — untouched. FIX-2A PR #63 OPEN/DRAFT/base main — untouched.

## Why this phase (audit linkage)
The audit rated most provider failure/recovery scenarios FULL, but flagged that the strongest
coverage (multi-cycle backoff, single-owner, shutdown-during-recovery) lived on the **legacy**
`LiveMarketRuntime`, while the decoupled `ProviderSupervisor` (what H9 ships) had thinner assertions.
H8E brings the decoupled path to parity for the residual gaps: **Gap 4** (decoupled multi-cycle
backoff unasserted), **Gap 7** (burst-immediately-after-reconnect), and **Gap 9** (decoupled
single-stream-loop / idempotent-start not directly asserted), plus the **§13** epoch-across-reconnect
focus.

## Topology (real go-forward composition; fake only at the provider boundary)
```
_CountingReconnectingProvider (no Dhan/token/socket/internet)
   -> MarketIngestionService + ProviderSupervisor          (real)
   -> build_publication_stack: M1 -> M2 -> D1 -> L1          (real composition root)
   -> Redis md:events (redislite)
   -> MarketEventConsumer + durable C1 -> RecordingShadowSink -> H4C comparator
```
The provider is a local fake with a scripted recoverable drop (`cut()` -> `ConnectionError`) and an
active-stream concurrency counter. Every internal component is the real production class. Backoff is
driven through an injected recording sleeper; correctness is asserted on the recorded delay **values**
and on `compare()` parity, never on elapsed wall-time.

## Bug found + fix
`MarketIngestionService.start()` had **no idempotency guard**. `boundary.start()` and
`publisher.start()` are both idempotent (`return` on re-entry, so no epoch is re-allocated), but a
second `start()` still ran `_start_provider()` again — a second `provider.connect()`, a second
`ProviderSupervisor`, and a second `_supervisor_task` (orphaning the first, running **two** concurrent
provider stream loops), plus a second observer/watch task. Reproduced tests-first:
`test_h8e_c_*` observed `connect_calls == 2` / `max_active_streams == 2` before the fix.

**Fix (smallest owning-layer correction):** a guard at the top of `start()` —
`if self._status is not ServiceStatus.NOT_STARTED: return` — placed so the guard and the `STARTING`
transition run with no intervening `await`, making it safe against both repeated **and** concurrent
starts. Matches the sibling idempotent-`start()` pattern. Diff = **+8/-0 in one file**
(`app/market_ingestion/service.py`). Mutation-verified: disabling the guard fails both `test_h8e_c_*`.

## Test evidence
`backend/tests/integration/test_market_ipc_h8e_supervisor_assertion_parity_redis.py` (redislite; fake
provider; real decoupled composition):

| requirement | test | real components | fake boundary | key assertions |
|---|---|---|---|---|
| Gap 4 + §13 — multi-cycle recoverable backoff, epoch stable, clean continuation | `test_h8e_a_multicycle_recoverable_backoff_and_epoch_stable` | service+supervisor+M1/M2/D1/L1+consumer+C1 | provider | 3 cuts → recorded backoff `[1.0, 2.0, 4.0]`, all `0 < d ≤ 30` (bounded, no tight loop); `reconnect_total==3`; status RUNNING; `terminal_failure` False; `epoch == epoch0`; drained parity `is_clean`, `matched==40`, missing/unexpected 0; clean stop → STOPPED |
| Gap 9 — one active stream loop across reconnects | `test_h8e_b_exactly_one_active_stream_loop` | service+supervisor | provider (concurrency counter) | over 4 reconnect cycles `max_active_streams==1`; `connect_calls==1`; `stream_calls==5`; post-stop `current_active_streams==0` |
| Gap 9 — repeated start cannot create a 2nd loop | `test_h8e_c_repeated_start_is_idempotent_no_second_loop` | service+supervisor | provider | after a 2nd sequential `start()`: `connect_calls==1`, `max_active_streams==1`, `epoch==epoch0`, RUNNING; events still flow |
| Gap 9 — concurrent start cannot create a 2nd loop | `test_h8e_c_concurrent_start_is_idempotent_no_second_loop` | service+supervisor | provider | `asyncio.gather(start(), start())` → `connect_calls==1`, `max_active_streams==1`, RUNNING |
| Gap 7 — burst immediately after reconnect | `test_h8e_e_burst_immediately_after_reconnect` | full chain + consumer/C1 | provider | 30 initial → cut → reconnect → 500-event burst; `epoch==epoch0`; `reconnect_total==1`; drained parity `is_clean`, `matched==530`, missing/unexpected/value_mismatch 0; `applied_total==530` |

## Omitted / optional (per spec §9/§11/§12)
- **Test D (`_live_receive_lock` contention): omitted, documented.** That lock serialises the Dhan
  adapter's receive path across *multiple* consumers of one adapter (the legacy in-process fan-out).
  The decoupled `ProviderSupervisor` drives a *single* consumer, so on the go-forward path Test B
  already proves the single-stream-loop invariant; the lock remains structurally in place
  (`adapters/dhan/adapter.py`, `async with self._live_receive_lock`) and covered by the existing
  adapter suites. A dedicated two-consumer test would exercise a legacy-path concern orthogonal to
  H8E's decoupled focus.
- **Optional shutdown-during-recovery on redislite / stale-through-consumer-wire:** not added. The
  audit already rated shutdown-during-recovery and stale/out-of-order FULL; adding them here would
  only raise test count without a new contract.

## Determinism
No test gates correctness on a real sleep: supervisor backoff uses an injected recording sleeper
(`asyncio.sleep(0)`); event/reconnect waits are condition-gated polls with a large iteration cap used
only as deadlock protection; parity is asserted via `compare()`. (The L1 observer's ~5 ms background
tick and the 1 ms poll cadence are present but non-gating — no assertion depends on elapsed
wall-time; consumer `block_ms=0` so reads never block.)

## Regression
- H8E targeted: 5 passed.
- Lifecycle/service + H5/H6/H7 + shadow-publish-failures: 126 passed.
- Full suite: 2708 passed, 9 skipped (was 2703 at H9A; +5 H8E), runtime ~175 s.
- ruff / ruff format / mypy(`service.py`): clean.

## Safety
Production contacted? NO · Real Dhan used? NO · Dhan HTTP? NO · Dhan WebSocket? NO · Credentials? NO ·
Orders? NO · Internet required? NO · FIX-2A touched? NO · H9B started? NO.

## Residual (unchanged by H8E — remain H9/H9B deliverables)
Cross-process ownership interlock **wiring** (`acquire→validate→connect`, `lose-lease→stop`), unique
per-incarnation `instance_id`, aware-UTC consumer `now` for the age gate, B11 loss-detector
composition + `md:health` conveyance, real provider binding, and live validation. H8E adds no
production capability beyond the idempotent-`start()` guard.
