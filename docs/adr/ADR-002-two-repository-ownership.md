# ADR-002 — Separate ApexScan into Backend and Frontend Repositories

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-03 |
| **Deciders** | ApexScan V1 Product Owner |
| **Supersedes** | The single-repository layout assumption in `docs/00_PROJECT_OVERVIEW.md` §9 and the single-repository local-composition assumption in `docs/10_DEPLOYMENT.md` §4.1 |
| **Superseded by** | — |
| **Related** | `docs/00_PROJECT_OVERVIEW.md`, `docs/04_FRONTEND_ARCHITECTURE.md`, `docs/10_DEPLOYMENT.md`, `docs/12_ROADMAP.md` |

---

## Context

ApexScan was initially laid out as one repository containing `backend/`,
`frontend/`, shared Docker Compose configuration, and one combined CI
workflow. The V1 repository decision is now two independently cloneable and
testable repositories:

- `apexscan-backend`
- `apexscan-frontend`

This changes repository and deployment ownership only. It does not alter the
Data Provider, Market Engine, Strategy Engine, database, API, WebSocket,
dependency-rule, or strategy plug-in architecture.

## Decision

### Backend repository

`apexscan-backend` owns the FastAPI application, backend tests, PostgreSQL,
Redis, Alembic migrations, backend Docker image/configuration, backend and
infrastructure Compose, backend scripts, backend CI/security checks, backend
environment configuration, canonical architecture documentation, and ADRs.

Its local Compose stack contains FastAPI, PostgreSQL, Redis, and
backend-owned supporting infrastructure only. It must not build, mount, clone,
or otherwise depend on a sibling frontend checkout.

### Frontend repository

`apexscan-frontend` owns the React application at its repository root,
TypeScript/Vite configuration, frontend tests, package files, frontend CI,
npm audit, frontend environment configuration, production build, and
frontend-only static-serving configuration.

The frontend communicates with the backend solely through configured HTTP API
and WebSocket base URLs. It does not rely on a sibling backend checkout.

### Host-level Nginx

The existing Nginx configuration routes static frontend traffic and backend
HTTP/WebSocket traffic. It is therefore host-level deployment infrastructure,
not frontend-only static-serving configuration. It remains system-owned in
`apexscan-backend/docker/nginx/` alongside canonical deployment
documentation; it is not part of the backend's local Compose stack and must
not use a sibling repository filesystem path.

### CI and environment contracts

Backend CI owns Python 3.13, Ruff, mypy, pytest, pip-audit, and backend
Docker/Compose validation. Frontend CI owns Node 22.22.0, npm ci, ESLint,
TypeScript, configured frontend tests, production build, and npm audit. No CI
workflow clones the other repository to run its quality gates.

Backend and frontend each own a separate environment example. Backend values
cover runtime, CORS, PostgreSQL, Redis, and API configuration. Frontend values
cover browser-exposed `VITE_` API and WebSocket base URLs only.

## Consequences

**Positive**

- Both applications can be cloned, developed, tested, and validated
  independently.
- The repository boundary reinforces the existing UI-to-API dependency
  direction without changing any domain boundary.
- Frontend and backend dependency/security gates are isolated and reviewable
  in their respective repositories.

**Trade-offs**

- A local developer starts the backend/infrastructure stack and Vite server
  separately instead of using one combined Compose file.
- The host-level Nginx configuration coordinates independently built frontend
  and backend deployment artifacts; local backend Compose does not attempt to
  replicate that production edge topology.
- Changes spanning the API contract and UI require coordinated PRs across two
  repositories.

## Guardrails established by this decision

- No repository uses a sibling-repository path such as `../apexscan-frontend`
  in source, Docker contexts, volume mounts, scripts, or CI.
- `apexscan-frontend` is never nested beneath a `frontend/` directory inside
  its own repository.
- The existing frontend repository's independent history, `.gitignore`, and
  `LICENSE` are preserved.
- The current backend `frontend/` directory is removed only after a validated
  frontend import exists in the frontend repository.
- No frozen architecture document is rewritten to restate this decision; this
  ADR is the superseding V1 repository/deployment record.

---

*This ADR records a point-in-time decision. If it is ever revised, mark it
`Superseded by` a new ADR rather than editing the decision in place.*
