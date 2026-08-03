# Security Exceptions

This operational register records accepted upstream security exceptions. It
does not amend the frozen architecture documents or waive a Phase 1 quality
gate unless the applicable roadmap explicitly permits that waiver.

## SEC-001 — React Router RSC-mode CSRF advisory

| Field | Value |
|---|---|
| Status | Resolved |
| Recorded | 2026-08-03 |
| Previously affected packages | `react-router-dom@7.18.2`, `react-router@7.18.2` |
| Advisory | [GHSA-qwww-vcr4-c8h2](https://github.com/advisories/GHSA-qwww-vcr4-c8h2), npm advisory source `1124282` |
| Severity | High |
| Audit finding | `npm audit --audit-level=high` reports two affected package entries from the one underlying advisory. |
| Affected range | `>=7.12.0 <8.3.0` |
| Latest published package version checked | `7.18.2` on 2026-08-03 |

### Original classification

The installed version at classification time was the latest version npm
reported as published, but it fell inside the advisory's affected range. The
audit proposed `react-router-dom@7.11.0` as a breaking change; a full audit of
that version also reported high-severity React Router advisories.

### Resolution

React Router `8.3.0` became available as the patched release. ApexScan uses
Data Mode only, without RSC, framework, or unstable APIs, so the migration was
limited to package replacement and documented import-path changes:

- `react-router-dom` was removed and `react-router@8.3.0` installed.
- `RouterProvider` now imports from `react-router/dom`.
- `createBrowserRouter`, `Link`, `NavLink`, and `Outlet` now import from
  `react-router`.
- CI now pins Node.js `22.22.0`, the documented React Router v8 minimum.

On 2026-08-03, `npm ci`, lint, TypeScript type-checking, production build, and
`npm audit --audit-level=high` all completed successfully; the audit reported
zero vulnerabilities. The CI audit remains fail-closed and is not suppressed.

### Effect on Phase 1

The upstream security issue no longer blocks Phase 1. Remaining Phase 1
blockers are environment-bound Docker, Python 3.13 backend validation, and
runtime PR/CI execution.
