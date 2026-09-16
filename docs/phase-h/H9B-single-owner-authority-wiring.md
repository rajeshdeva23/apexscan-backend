# H9B — single-owner authority wiring & offline cutover proof

**Status:** OFFLINE implementation complete. Composes the proven H9A ownership primitive, the B11
loss detector, and md:health conveyance into the real production-shaped runtime, and proves offline
that **peak live-provider owners == 1** across startup, restart, cutover, rollback, crash/TTL
takeover, and ownership loss. **No real Dhan, no production deploy, no IPC-authoritative switch, no
live cutover** — those remain the separately-governed H9C (see the cutover runbook).

## Baseline
- Base: `feature/decoupling-hardening` `eb2c23b` (post-H8E). Branch:
  `feature/phase-h9b-single-owner-authority-wiring`.
- Production `main` `a6b8c68` — untouched. FIX-2A PR #63 OPEN/DRAFT/base main — untouched.

## Design decision — md:health carries the B11 L1 evidence (one health truth)
The H9B preflight found a contract gap: `IngestionHealthState` (the `md:health` model) did not carry
the three producer L1 facts B11 needs. Resolved by **Option A — extend `IngestionHealthState`** with
`last_published_sequence`, `terminal_publication_break`, `publication_outcome_uncertain`. The producer
writes them directly from the same `FeedContinuitySnapshot` that
`ProducerPublicationEvidence.from_continuity` reads, and the backend re-projects them via
`ProducerPublicationEvidence.from_ingestion_health`. There is exactly ONE health truth at
`md:health`; no competing continuity record is introduced. This extends a Phase-A "future" model that
was never wired, so it changes no accepted ADR contract.

## Ownership lifecycle (ADR-030)
`ProviderOwnershipGuard` (`app/market_ingestion/ownership_runtime.py`) is the missing orchestration
layer around the stateless H9A `RedisOwnershipCoordinator`. One guard per runtime **incarnation**:

```
acquire  -> validate  -> (permit token mint / provider connect)
         -> renew on a bounded, injected-clock cadence (deterministically testable)
         -> ownership loss (renew/validate fails) => mark lost + fail-closed notification + lost-event
         -> release only on a clean stop, and only a lease this incarnation still owns
```

Hard invariants: a stale owner never renews; a lost lease is **never** released (releasing after
loss could delete a newer owner's fencing state — the release Lua is fenced, and the guard also
refuses structurally); the fail-closed notification is synchronous and lightweight (it sets a
terminal event), so it never cancels the renewal loop from within itself.

### Instance identity (three independent identifiers, never overloaded)
- `instance_id` — `uuid4().hex`, one per guard/incarnation; answers "same owner?". A provider
  reconnect keeps the guard → same `instance_id`; a process restart builds a new guard → new one.
- `producer_epoch` — DurableEpochAllocator file; the dedup/lineage identity.
- `fencing_generation` — Redis `INCR`; monotonic stale-owner rejection.

## Critical ordering invariant (both paths)
```
acquire fenced ownership -> validate -> ONLY THEN token mint / provider connect
```
Enforced twice on the decoupled path (defence in depth): `acquire_or_fail` before publication/provider
startup, and a belt-and-braces `validate()` immediately before the provider connect. On the legacy
path, `_acquire_backend_ownership` runs before `coordinator.start()` (the token mint).

## Per-change record (requirement · owning layer · why · test · rollback)
| # | Production change | Owning layer | Why | Test | Rollback impact |
|---|---|---|---|---|---|
| 1 | `ProviderOwnershipGuard` + `build_provider_ownership_guard` | `market_ingestion/ownership_runtime.py` (new) | Lifecycle the H9A primitive lacks | `test_ownership_runtime.py` | New module; unused when ownership off |
| 2 | `market_ownership_enabled` + lease timing + `market_ownership_config()` | `core/config/settings.py` | From-settings lease contract; fail-fast timing | `test_settings.py` (timing) + wiring tests | Default OFF → no behaviour change |
| 3 | Decoupled service ownership wiring (acquire→validate→connect, renewal, loss→fail-closed, release, reconnect guard) | `market_ingestion/service.py` | Single-owner gate on the go-forward path | `test_market_ipc_h9b_two_path_single_owner_redis.py` | `ownership=None` → prior behaviour |
| 4 | Supervisor `reconnect_guard` | `market_ingestion/supervisor.py` | A stale owner must not reconnect | lease-loss + valid-reconnect tests | `reconnect_guard=None` → prior behaviour |
| 5 | Legacy path ownership wiring (acquire before mint, release on shutdown, loss→shutdown watcher) | `services/dhan_runtime_composition.py` | The legacy path had zero cross-process ownership | two-path tests | `ownership=None` → prior behaviour |
| 6 | `IngestionHealthState` +3 B11 fields | `market_ipc/state.py` | Convey producer L1 evidence to B11 | health round-trip tests | Additive optional fields |
| 7 | `IngestionHealthPublisher`/`Reader` + `ingestion_health_from_continuity` + `from_ingestion_health` | `market_ipc/health.py` (new), `loss_detection.py` | md:health writer/reader, fail-closed staleness | health round-trip + B11 tests | New module; unused until composed |
| 8 | Consumer last-applied `(epoch, sequence)` + `consumer_progress()` | `market_ipc/consumer.py` | B11 consumer-progress evidence | `test_..._h9b_health_b11_redis.py` | Additive; advances only on durable apply (H8A preserved) |
| 9 | B11 composed in the runtime + `evaluate_authority_readiness()` | `market_ipc/consumer_runtime.py` | Readiness INPUT (activates nothing) | B11 composed tests | Additive; inert runtime → fail-closed |
| 10 | Backend-root consumer-runtime wiring (one shared aware-UTC clock) | `services/backend_consumer_runtime.py` (new), `main.py` | H9B §17 | full-suite startup + architecture boundary test | Inert under default flags |
| 11 | `health_ttl_seconds` / `health_stale_seconds` | `market_ipc/config.py` | md:health TTL + reader staleness deadline | health tests | Additive with defaults |

## md:health schema (added fields)
`last_published_sequence: int | None`, `terminal_publication_break: bool`,
`publication_outcome_uncertain: bool`. Writer sets a TTL (`health_ttl_seconds`); reader fails closed
on missing / malformed / stale (`abs(now - updated_at) > health_stale_seconds`) — a stale producer
position is never read as current.

## Single-owner safety (offline-proven)
- **startup** — acquire+validate before connect on both paths; the loser fails closed (0 connects).
- **restart** — a new incarnation acquires a strictly higher fence; a returning stale owner is fenced.
- **lease loss** — renew/validate failure trips fail-closed: provider disconnect, no reconnect.
- **cutover (legacy→decoupled)** — stop legacy → prove disconnect → release → decoupled acquires →
  connect; a 0-owner gap is allowed, a 2-owner overlap is asserted impossible.
- **rollback (decoupled→legacy)** — symmetric; fence rises.
- **crash/TTL takeover** — a crashed owner's lease blocks a successor until it expires, then the
  successor acquires a higher fence; the stale owner cannot return.

The two-path test drives the REAL `compose_market_runtime` (legacy) and the REAL
`MarketIngestionService` (decoupled) against one shared `redislite` authority with identical lease
keys, and a shared owner tracker asserts `active_provider_owners <= 1` on every connect.

## Determinism
Guard renewal is stepped through an injected permit sleeper (unit test); no correctness assertion
depends on elapsed wall-time. Crash/TTL expiry is modelled deterministically by the owner key
vanishing (equivalent to a real TTL expiry) rather than a real-time wait. Lease loss during
operation is driven by an explicit stream cut, not by renewal timing.

## Test evidence
- `tests/unit/test_ownership_runtime.py` — 9 (identity, incarnation, acquire/conflict, renewal→loss,
  validate→loss, wait_lost, release discipline, never-release-lost).
- `tests/integration/test_market_ipc_h9b_health_b11_redis.py` — 17 (md:health round-trip across
  healthy/break/uncertain + missing/stale/malformed/TTL; B11 composed: healthy/lagging/producer-break/
  missing-health/redis-loss/inert; consumer progress; evidence equivalence).
- `tests/integration/test_market_ipc_h9b_two_path_single_owner_redis.py` — 7 (token-mint guard both
  directions; cutover; rollback; crash/TTL; lease-loss no-reconnect; valid reconnect permitted).
- Mutation-verified: removing the supervisor `reconnect_guard` fails the lease-loss test.

## Safety
Production contacted? NO · Real Dhan? NO · Dhan HTTP/WS? NO · Dhan token generated? NO · Credentials?
NO · Orders? NO · IPC authoritative enabled? NO · `main` changed? NO · FIX-2A touched? NO · Live
cutover? NO. Production default is ownership OFF (`market_ownership_enabled=false`) — the wiring is
inert until a governed cutover enables it.

## Residual (H9C / follow-up — LIVE-gated)
First real Dhan token mint / WS, the live cutover feed-gap measurement, live parity, deploying the
interlock to both services, enabling `ipc_authoritative`, and the roadmap prerequisites (ADR-028/029
acceptance, FIX-2 RC3 resolution, reference bootstrap on cutover). See
[H9B-single-owner-cutover-runbook.md](H9B-single-owner-cutover-runbook.md).
