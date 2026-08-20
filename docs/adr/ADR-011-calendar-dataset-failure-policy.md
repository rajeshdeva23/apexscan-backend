# ADR-011 Addendum — Authoritative Calendar-Dataset Load-Failure Policy

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window |
| **Closes** | The ADR-011-LIVE-CALENDAR-IMPL-R1 residual (enabled runtime + dataset-load failure retaining a legacy live classifier) |
| **Related** | ADR-010 (runtime lifecycle & credential-absence policy D14), ADR-011 live out-of-coverage addendum (LC1–LC20), ADR-011 calendar-monitor governance (MON1), ADR-011-IMPL, ADR-011-CALENDAR-MONITOR-IMPL, ADR-004 |
| **Status** | Accepted (decision + implementation contract); **implementation DEFERRED** to ADR-011-DATASET-FAILURE-IMPL |
| **Date** | 2026-08-16 |
| **Deciders** | Market-Engine / Platform Architecture |
| **Decision** | **Option A — fail-fast composition/startup.** When the authoritative packaged trading-calendar dataset cannot be loaded/validated in the provider-enabled path, `compose_market_runtime` raises a governed `AuthoritativeCalendarUnavailableError`; no market-capable runtime is returned and no managed market task starts. No legacy/secondary calendar source is ever promoted to authority. |

---

## Context

ADR-011-IMPL routed completed-session historical planning onto the packaged
`TradingCalendarDataset`; ADR-011-LIVE-CALENDAR-IMPL-R1 unified the live
`MarketSessionClassifier` onto the same dataset (date-level authority + `CalendarCoverage`,
with `MarketState.CALENDAR_UNAVAILABLE` outside coverage). Both slices flagged one residual:
when the market provider is **enabled** but the packaged dataset cannot be loaded/validated,
historical warmup correctly becomes `UnavailableHistoricalWarmup`, but the **live classifier**
could silently revert to the legacy `settings.nse_holidays`-derived classifier. This slice
governs that failure.

## Residual — confirmed by code inspection

The hazard exists on the current path:

1. `compose_market_runtime` (`app/services/dhan_runtime_composition.py`) →
   `dataset = _resolve_calendar_dataset(calendar_dataset)`.
2. `_resolve_calendar_dataset` catches `(ValidationError, OSError)`, logs, and **returns `None`**
   (silent fail-closed for the historical half).
3. `_live_session_classifier(settings, dataset=None)` **returns `None`**.
4. `LiveMarketRuntime.__init__` executes `self._session = session_classifier or
   MarketSessionClassifier(schedule=schedule, calendar=calendar, exchange_timezone=…)`, where
   `schedule, calendar = _schedule_and_calendar(settings)` builds a `TradingCalendar` from
   `settings.nse_holidays` **with `coverage=None`**.

A `coverage=None` classifier never returns `CALENDAR_UNAVAILABLE`; with an empty
`nse_holidays` it classifies every weekday as a live/phase state and weekends as `HOLIDAY`.
Because the enabled path also starts the live-ingestion task, ticks would flow into the
`TickEngine` and could produce `LIVE_SESSION` and drive strategy progression on a
**non-authoritative** calendar. This is an **authority-restoration hazard** and is classified
as such.

### Failure-mode completeness (do not assume all failures are `ValidationError`)

| Failure mode | Raised by | Currently caught by `(ValidationError, OSError)`? |
|--------------|-----------|---------------------------------------------------|
| Packaged resource missing | `importlib.resources … read_text` → `FileNotFoundError` | Yes (OSError) → silent `None` (residual) |
| Malformed JSON | `model_validate_json` → `ValidationError` | Yes → silent `None` (residual) |
| Coverage inverted / OPEN∩CLOSED / bad interval / provenance | model validators → `ValidationError` | Yes → silent `None` (residual) |
| Unexpected loader I/O | `OSError` subclass | Yes → silent `None` (residual) |
| **Corrupt non-UTF-8 bytes** | `read_text(encoding="utf-8")` → `UnicodeDecodeError` (**a `ValueError`, not `OSError`**) | **No → currently propagates uncaught (crash)** |

So today the module has two inconsistent behaviours (silent `None` fallback vs. uncaught
crash). Option A collapses **all** dataset load/validate failures into one governed error.

## Non-negotiable invariant

> **Failure to load the authoritative dataset MUST NEVER promote a legacy or secondary
> source to calendar authority.** `settings.nse_holidays`, the Dhan market-holiday monitor,
> and weekday/weekend inference alone must never become authoritative fallbacks when the
> packaged dataset was expected but unavailable.

## Option assessment

- **Option A — fail-fast composition/startup. *Selected.*** Dataset load/validate failure in
  the enabled path raises `AuthoritativeCalendarUnavailableError`; no market-capable runtime is
  returned; zero managed market tasks start; no classifier fallback. Strongest fail-closed
  posture, smallest runtime-state surface, and it *reuses the existing mandatory-dependency
  path* — `main.py` wires `LiveMarketRuntimeDependency` into `ApplicationLifecycle.provider`
  only when `market_provider_enabled`, so a raise propagates through `provider.start` →
  `ApplicationLifecycle.start` → `FAILED`/`ApplicationStartupError`, exactly as ADR-010 D14
  handles enabled-provider credential failure. No new readiness architecture. Trade-off: in
  enabled mode a dataset integrity fault fails application startup — the correct posture for a
  packaged, version-controlled, deployment-time artifact.
- **Option B — degraded runtime, live always `CALENDAR_UNAVAILABLE`. *Rejected.*** Would require
  broadening `CALENDAR_UNAVAILABLE` from "date outside coverage" (LC2/LC8) to "authority
  unavailable", plus a way for a classifier to return it for *every* date without an invalid
  sentinel `CalendarCoverage` (which §7 forbids). It starts a market subsystem that can never
  trade — more runtime complexity for strictly weaker safety than A. Safe but wasteful.
- **Option C — app healthy, market subsystem unavailable. *Rejected (for now).*** The existing
  architecture makes the enabled runtime a *mandatory* dependency, so A already yields the
  correct application outcome (startup FAILED, liveness still "live") without new work. A
  distinct "app up / market subsystem down but not failed" readiness state does not exist and
  would be a new subsystem (§8/§22 caution). If operators later want that, it is a separate,
  explicitly-governed readiness decision — not required to close this residual.
- **Option D — legacy `settings.nse_holidays` fallback. *Rejected.*** Directly violates the
  single-authority model: LC15 demotes `nse_holidays` to legacy/non-authoritative for the
  dataset-enabled path; D5/D4 make an unbacked calendar non-authoritative. A fallback would
  silently restore the exact hazard this slice closes.
- **Option E — Dhan monitor fallback. *Rejected.*** MON1 makes the Dhan page secondary /
  review-only; it must never construct authority, write the dataset, or enable classification.

## Decisions DF1–DF16

- **DF1 — Selected policy.** Option A. In the provider-enabled composition path, any failure to
  load or validate the authoritative dataset is a fail-fast composition/startup error.
- **DF2 — Governed error.** Introduce `AuthoritativeCalendarUnavailableError` (name provisional;
  confirm against the error taxonomy at IMPL). It is a composition/startup error, **distinct**
  from provider-credential/lifecycle errors and from `UniverseResolutionError`. It wraps the
  underlying cause (`raise … from error`) and preserves it. Missing vs. malformed vs. invalid
  datasets all map to this one error (message may distinguish them for diagnostics; behaviour is
  identical).
- **DF3 — Broad failure capture.** The resolver must treat **every** load/validate failure as
  fail-fast, not only `(ValidationError, OSError)`: include the `UnicodeDecodeError`/`ValueError`
  corruption modes. No silent `None` fallback survives in the enabled path.
- **DF4 — No fallback of any kind.** On dataset failure the composition must not consult
  `settings.nse_holidays`, the Dhan monitor, or a weekday/weekend-only calendar. There is no
  `if failed: use settings` branch and no secondary-source promotion.
- **DF5 — Shared authority boundary.** Historical warmup and the live classifier share **one**
  success/failure boundary: either the dataset resolves (both get dataset authority) or
  composition fails (neither is built). The prior split — historical `UnavailableHistoricalWarmup`
  while live kept a settings classifier — is eliminated.
- **DF6 — No market-capable runtime on failure.** On failure `compose_market_runtime` returns no
  runtime; `runtime.start()` is never reached; **zero** managed market tasks (ingestion,
  session-statistics refresh, calendar monitor) are created (ADR-010 D9). Any already-started
  provider coordinator is cleaned up on the raise path (mirroring the existing
  `_safe_shutdown(coordinator)` unwinding), so no provider connection and no task leaks.
- **DF7 — Application outcome (enabled).** Via the existing `provider` seam, the raise makes
  `ApplicationLifecycle.start` fail: state `FAILED`, `ApplicationStartupError`, readiness
  `not_ready`. Liveness stays "live" (process up) under the existing `liveness_snapshot`. No new
  health/readiness contract is introduced (ADR-010 D13/D14 reused).
- **DF8 — Disabled mode unchanged.** When `market_provider_enabled` is false the runtime is
  dormant (no provider dependency wired in `main.py`, no dataset resolved, no live data, no
  monitor). The disabled runtime's settings-derived `coverage=None` classifier is **out of
  live-trading authority scope** (it classifies no live data) and is left as-is. This slice
  governs only the enabled path.
- **DF9 — `CALENDAR_UNAVAILABLE` semantics unchanged.** `MarketState.CALENDAR_UNAVAILABLE`
  remains scoped to "the instant's trading date lies outside `CalendarCoverage`" (LC2/LC8). It is
  **not** broadened to mean "authority unavailable" — Option A needs no such runtime state
  because no runtime starts without authority. LC1–LC20 semantics are preserved verbatim.
- **DF10 — Startup vs. runtime failure.** (A) Dataset fails during composition → DF1 fail-fast.
  (B) Dataset already loaded into immutable in-memory objects, then the packaged file later
  disappears/corrupts on disk → **no runtime effect**: the calendar authority is the in-memory
  `TradingCalendarDataset`/`TradingCalendar`/`CalendarCoverage`, never re-read after startup. **No
  periodic authoritative-dataset reload is introduced** (D21 restart-rebuild semantics suffice).
- **DF11 — Market-ingestion safety.** Under A, on failure nothing starts: no ticks reach the
  `TickEngine`/`CandleEngine`/`SessionStatistics`/`StrategyManager`, and `LIVE_SESSION` can never
  be produced from an inferred calendar. On the success path, behaviour is exactly today's.
- **DF12 — Monitor behaviour.** The Dhan monitor stays secondary. It is not started when
  composition fails (DF6). Where it does run against a `None`/unresolved dataset (defensive), it
  yields `AUTHORITATIVE_COVERAGE_MISSING` and never constructs authority, writes the dataset,
  enables live classification, or enables historical warmup (MON1/MON6).
- **DF13 — `settings.nse_holidays` final role.** In the enabled/production path it is **no longer
  reachable as authority** (A removes the only path that used it — the failed-dataset fallback;
  the success path always injects the dataset-backed classifier). It is retained **only** as the
  disabled-runtime schedule/calendar input and its tests. Full removal from production
  composition is a follow-up cleanup (consistent with LC15 "removal deferred"). It must never
  rescue a failed dataset.
- **DF14 — Current-day isolation.** Unchanged: `supports_current_day=False`; current-day withheld;
  `CURRENT_DAY_RECONCILIATION_GUARANTEE = NOT PROVEN`. This slice does not enable current-day
  reconciliation.
- **DF15 — Session-statistics authority isolation.** Unchanged: `staged_observation_verified=False`,
  `tick_aggregate_verified=False`. No relationship to P4.6E6C.
- **DF16 — Provider neutrality & live special hours.** No provider becomes authority during
  calendar failure; no Dhan type enters the Market Engine. Muhurat 2026-11-08 date-level authority
  remains proven; live intraday special hours remain separately unproven/governed (not solved
  here).

## Failure matrix (enabled path unless noted)

| Scenario | Composition outcome | Live calendar state | Historical warmup | Monitor behaviour | Strategy progression | Authority source |
|----------|---------------------|---------------------|-------------------|-------------------|----------------------|------------------|
| Dataset valid | succeeds | dataset classifier (`CALENDAR_UNAVAILABLE` outside coverage; phase within) | real warmup | compares vs. dataset | normal, within coverage | dataset |
| Dataset missing | **raise `AuthoritativeCalendarUnavailableError`** | none (no runtime) | none | not started | none | none (fail-fast) |
| Dataset malformed JSON | **raise** (same) | none | none | not started | none | none |
| Dataset validation failure (coverage/OPEN∩CLOSED/interval/provenance) | **raise** (same) | none | none | not started | none | none |
| Corrupt non-UTF-8 bytes | **raise** (same; DF3) | none | none | not started | none | none |
| Requested date outside coverage (dataset valid) | succeeds | `CALENDAR_UNAVAILABLE` (unchanged) | fail-closed outside coverage | `AUTHORITATIVE_COVERAGE_MISSING` for that date | none for that date | dataset |
| Dhan monitor unavailable (dataset valid) | succeeds | dataset classifier | real warmup | `DHAN_FETCH_FAILURE`; no authority effect | normal | dataset |
| `nse_holidays` empty | irrelevant to authority | dataset classifier (enabled) | real warmup | vs. dataset | normal | dataset |
| `nse_holidays` populated + dataset failed | **raise** — populated holidays **cannot rescue** | none | none | not started | none | none |
| Disabled mode (`market_provider_enabled=false`) | dormant bare runtime | settings `coverage=None` classifier, **no live data** (out of authority scope) | n/a | not wired | none (no ingestion) | n/a |

## Governed implementation contract (for ADR-011-DATASET-FAILURE-IMPL)

- Replace the enabled-path `_resolve_calendar_dataset` silent-`None`-on-failure with a resolver
  that **raises `AuthoritativeCalendarUnavailableError`** on any missing/malformed/invalid/corrupt
  dataset (DF2/DF3), wrapping the cause. An injected test dataset is still used verbatim.
- `compose_market_runtime` (enabled branch): resolve the dataset **before** returning a runtime;
  on failure clean up the already-started provider coordinator (existing `_safe_shutdown` pattern)
  and re-raise. No `LiveMarketRuntime` is constructed/started; zero market tasks created (DF6).
- Remove the `_live_session_classifier`→`None`→settings-classifier fallback for the enabled path:
  the enabled path either injects a dataset-backed coverage-aware classifier or fails composition
  (DF4/DF5). `LiveMarketRuntime`'s `session_classifier or _schedule_and_calendar(...)` default is
  exercised **only** by the disabled/no-provider path (DF8).
- Do not broaden `CALENDAR_UNAVAILABLE` (DF9). Do not add periodic dataset reload (DF10).
- Preserve invariants DF14/DF15/DF16. Historical algorithms, `CalendarCoverage`,
  `OutsideCalendarCoverageError`, `MissingSessionTimingError`, and the `CandleEngine`
  `SessionSchedule` are untouched.
- Confirm the error name/placement against the existing taxonomy at IMPL; keep it distinct from
  provider-credential/lifecycle errors and `UniverseResolutionError`.

## Future test matrix (for the implementation slice)

A. valid packaged dataset → composition succeeds (unchanged). B. missing packaged dataset →
`AuthoritativeCalendarUnavailableError`, no runtime. C. malformed JSON → same. D. dataset
`ValidationError` → same. E. no `settings.nse_holidays` fallback. F. populated `nse_holidays`
cannot rescue a failed dataset. G. Dhan monitor cannot rescue failed authority. H. no
`LIVE_SESSION` produced from failed authority (no runtime/ingestion exists to produce it). I. no
strategy progression. J. no orphan tasks; provider coordinator cleaned up. K. valid-dataset path
unchanged. L. historical warmup valid path unchanged. M. `CALENDAR_UNAVAILABLE` outside coverage
unchanged. N. `supports_current_day=False`. O. authority bits `False`. P. Market Engine
provider-neutral. Plus: corrupt non-UTF-8 bytes → `AuthoritativeCalendarUnavailableError` (DF3);
disabled mode still composes a dormant runtime (DF8).

## Files changed
Docs-only: this addendum + the ADR README index row. **No `app/` or `tests/` changes** in this
governance slice.

## Consequences
**Positive.** The last ADR-011 live-calendar safety residual is closed: an authoritative-dataset
integrity failure can never silently restore a legacy or secondary calendar, and historical and
live now share one authority success/failure boundary. **Negative / accepted.** In enabled mode a
dataset integrity fault fails application startup — a deliberate fail-closed posture for a
packaged, version-controlled artifact, not a transient market condition. **Neutral.** No code
this phase.

## Exact next slice
**ADR-011-DATASET-FAILURE-IMPL** — implement Option A per the contract above (governed error,
broad failure capture, removal of the enabled-path fallback, provider-coordinator cleanup on the
raise path), with the full test matrix and all gates. The `settings.nse_holidays` production-path
removal remains a later cleanup (DF13).
