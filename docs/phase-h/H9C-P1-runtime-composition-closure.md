# H9C-P1 — runtime composition closure (md:health + reference bootstrap)

**Status:** OFFLINE implementation complete. Closes the two implemented-but-unwired pipelines the
post-H9B live-readiness audit flagged (Gate J, Gate D). No Dhan, no production, no ownership enable,
no `ipc_authoritative` switch, no cutover. All proven against `redislite` + a fake provider.

## Baseline
- Branch `feature/phase-h9c-p1-runtime-composition-closure` off `feature/decoupling-hardening`
  `59c297e` (post-H9B). Production `main` `a6b8c68` and FIX-2A PR #63 untouched.

## Gate J — md:health producer writer wired into the ingestion runtime
The writer components (`IngestionHealthPublisher`, `ingestion_health_from_continuity`) existed since
H9B but had zero runtime callers. They are now driven by the **existing L1 observer** — no new
polling loop:

```
MarketIngestionService._run_observer (publisher mode, existing tick loop)
  -> continuity.observe_boundary(...)          (existing)
  -> _publish_health()                          (NEW): ingestion_health_from_continuity(snapshot)
  -> IngestionHealthPublisher.publish(state)    -> md:health (SET, TTL)
```

- **Seam:** the observer is the natural lifecycle. It is time-driven (`observer_sleep`), so a
  healthy-but-idle feed keeps `updated_at` fresh without any market tick — reconciling
  `health_ttl_seconds=30` against `health_stale_seconds=15` (a fresh record is never evicted early).
- **Write lifecycle:** first observer tick (startup) → each tick (normal + idle refresh) → the
  BROKEN-state tick before the observer returns (terminal break conveyed) → a final record on clean
  `stop()` after M2 drains (a non-healthy `ingestion=DOWN` snapshot, before the Redis client closes).
  A provider reconnect keeps the same incarnation epoch, so md:health's `producer_epoch` is stable
  across it; an ingestion restart is a new incarnation → a strictly higher epoch in md:health.
- **Failure policy (fail-closed, no storm):** a failed `publish()` (RedisError → `False`) is logged
  once per failure streak and **never** trips terminal. Health-observability loss is not read as
  healthy — the backend reader already fails closed on missing/stale/malformed — and it cannot cause
  a crash/restart storm.
- **Composition:** `_build_health_publisher` in `market_ingestion/composition.py` builds the writer
  over the **publication stack's** Redis client (one client per incarnation, closed by the service on
  shutdown); it is `None` in provider-only mode. Construction is confined to that seam (arch test).
- **Known limitation (pre-existing, out of P1 scope):** `IngestionHealthState.last_published_sequence`
  is conveyed faithfully from continuity, but the live continuity leaves it `None` — the M2 boundary
  exposes a published *count*, not a published *sequence*, so `observe_boundary` confirms publishes
  without a watermark. B11 does **not** depend on it (it derives the producer position from Redis
  stream metadata; the combined test reaches HEALTHY with the field `None`). Tracking a published
  sequence would require a boundary/D1-path change and is deliberately deferred. The producer
  position that advances live is `last_accepted_sequence`.

## Gate D — reference bootstrap wired into the consumer runtime
`ReferenceStateLoader` / `RedisCompactedReferenceStore` existed since Phase D but had zero runtime
callers. The consumer runtime now rehydrates durable reference **before** any event applies:

```
MarketEventConsumerRuntime.start()
  -> _bootstrap_reference()   (NEW): loader.load(current_trading_date, universe_version)
       -> self._reference_snapshot = snapshot
       -> sink.seed_reference(snapshot)   (if the sink supports it; duck-typed)
  -> consumer.start()          (idempotent XGROUP CREATE; existing)
  -> poll loop                 (existing)
```

- **Safety by construction:** the loader reads only `md:reference:<date>` — a key wholly separate
  from the stream, group, and PEL. It never ACKs an entry, discards a pending entry, resets the
  group (`ensure_group` swallows BUSYGROUP), or rewinds durable consumer progress. Existing PEL
  entries are recovered by the normal poll/reclaim path after bootstrap.
- **No Dhan:** the whole point is to recover `previous_close` / session OHLC from durable Redis
  state without re-authenticating; the consumer runtime imports no provider surface (arch test).
- **Producer epoch:** bootstrap never invents or rewrites the producer epoch; the consumer applies
  the legal epoch it reads and its last-applied progress reflects that epoch.
- **Fail-closed:** a missing trading-date authority → no bootstrap (warming up, seeds nothing
  false); an empty hash → an empty `warming_up` snapshot; a Redis outage during load propagates so
  `start()` fails closed (FAILED, client closed) rather than mistaking an outage for an empty
  session.
- **Composition:** `compose_consumer_runtime` builds the loader over the runtime's Redis client and
  threads the existing trading-date / universe-version authorities. Construction is confined to that
  seam (arch test), reachable only from the sanctioned `services/backend_consumer_runtime.py`.
- **Seeding is inert in the default path (INFO):** the snapshot is exposed via the
  `reference_snapshot` property and seeded into the sink only if the sink exposes `seed_reference`
  (duck-typed). The default `RecordingShadowSink` / `ShadowMarketEventSink` protocol has no such
  method, so in the shipping composition the recovered reference is loaded and observable but not yet
  consumed by an authoritative sink — that sink lands in a later phase. The bootstrap load itself
  (read-only on `md:reference`) runs regardless.

## Known limitations (deferred, out of P1 scope)
- **`_fail_closed` writes no final DOWN md:health record.** `stop()` publishes a final non-healthy
  record, but the fail-closed path only cancels the observer. Under P1 defaults this is inert:
  ownership is OFF, so `_fail_closed` is reached only via a continuity-BROKEN terminal, which the
  observer already conveys (it publishes the BROKEN state before returning). The only gap is a
  future H9C ownership-loss trip (continuity stays HEALTHY): the last record reads HEALTHY until the
  next `stop()` or a new owner overwrites it, bounded by the reader's `health_stale_seconds` gate.
  The DOWN-on-lease-loss semantics belong with H9C ownership enablement and are deferred there.
- **Arch-guard `class Name:` exclusion is substring-based.** A hypothetical evading module would have
  to also contain the literal class-definition line; this matches the pre-existing consumer guards
  and is acceptable (deliberate evasion required). The guards are genuinely tighter than before
  (each pins exactly one sanctioned seam).
- **`observer_interval_seconds < health_stale_seconds` is not cross-validated.** Idle-refresh
  freshness relies on the 0.5s default observer tick being far below the 15s staleness deadline;
  production never overrides it. A config cross-check is a pre-existing knob, not introduced here.

## Combined effect (the audit's blocker)
With the producer writing md:health at runtime and the consumer bootstrapping reference, B11
`evaluate_authority_readiness()` **no longer returns permanently `INSUFFICIENT_EVIDENCE`**: given
live-shaped producer evidence and a caught-up consumer it reaches HEALTHY / CONSUMER_LAGGING with
`ready_for_authority=True`. A backend restart rehydrates reference and re-derives dedup from the
durable authority (no duplicate application, no Dhan); an ingestion restart is reflected as a new
producer epoch in md:health and is not misread by B11 as a Redis loss.

## Architecture guardrails (updated narrowly)
- `test_reference_recovery_is_constructed_only_at_the_sanctioned_bootstrap_seam` — reference recovery
  may be built only in `market_ipc/consumer_runtime.py`; forbidden everywhere else.
- `test_health_publisher_is_constructed_only_at_the_ingestion_composition_seam` — the md:health
  writer may be built only in `market_ingestion/composition.py`.
- Existing boundaries preserved: the consumer runtime still imports no provider/publisher/epoch
  surface; domain layers stay IPC-unaware.

## Test evidence
- `tests/integration/test_market_ipc_h9c_p1_runtime_composition_redis.py` — 16 tests (Gate J: appear
  + TTL + identity, idle refresh, accepted-position advance, reconnect-epoch, terminal break,
  write-failure fail-closed; Gate D: load+seed, existing group+PEL preserved, epoch not rewritten,
  warming-up, load-outage fail-closed, no-date no-bootstrap, composition wires the loader; combined:
  health+reference+consumer+B11 sufficient, backend restart, ingestion restart no false B11 loss).
- Mutation-verified: disabling the runtime md:health writer fails the B11/health tests; disabling the
  reference bootstrap fails the load/compose tests. Both reverted.

## Safety
Production contacted? NO · Real Dhan REST/WS? NO · Token generated? NO · Deployed? NO · Runtime flags
changed? NO · FIX-2A touched? NO · Ownership/`ipc_authoritative` enabled? NO. Default flag shapes
compose an inert runtime (no Redis, no writer, no loader), so the wiring is inert until a governed
enable.
