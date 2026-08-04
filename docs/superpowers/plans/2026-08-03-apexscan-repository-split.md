# ApexScan Repository Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely separate the React application into `apexscan-frontend` while preserving both repositories' independent histories and keeping all Phase 1 validation ownership clear.

**Architecture:** `apexscan-backend` retains the FastAPI service, database/cache infrastructure, canonical architecture documentation, backend/infrastructure validation, and host-level Nginx deployment configuration. `apexscan-frontend` receives the contents of the present `frontend/` directory at its repository root and validates itself independently. ADR-002 supersedes the former monorepo and full-stack-local-Compose ownership assumptions: local backend Compose runs FastAPI, PostgreSQL, and Redis; local frontend development runs Vite independently.

**Tech Stack:** Python 3.13, FastAPI, PostgreSQL 17, Redis 7, Docker Compose, React 19, TypeScript, Vite, Node 22.22.0, GitHub Actions.

## Global Constraints

- Do not start Phase 2 or add trading, Market Engine, Strategy Engine, or Dhan functionality.
- Do not rewrite `apexscan-backend` history, force-push, copy `.git`, delete the frontend repository's existing history, or overwrite its `LICENSE`.
- Preserve the frontend repository's existing `.gitignore`; merge only required ignore rules.
- Do not move or remove `apexscan-backend/frontend/` until the copied frontend is committed and independently validated in `apexscan-frontend`.
- Do not modify frozen architecture documents (`docs/00_PROJECT_OVERVIEW.md` through `docs/13_ARCHITECTURE_GLOSSARY.md`). ADR-002 is the accepted, superseding record for repository and deployment ownership.
- Never commit `.env`, credentials, private keys, `node_modules`, `dist`, build output, virtual environments, Python caches, or editor-local files.
- The backend CI owns Python 3.13, Ruff, mypy, pytest, pip-audit, and Docker/Compose runtime validation. The frontend CI owns Node 22.22.0, npm CI, ESLint, TypeScript, tests when present, production build, and npm audit.

---

## Blocking prerequisites

- [ ] **Step 1: Obtain the actual frontend Git remote and verify access.**

  The inferred SSH address `git@github-personal:rajeshdeva23/apexscan-frontend.git` returned `Repository not found`; do not assume a replacement address. Obtain the exact clone URL from the repository owner, then run:

  ```bash
  git ls-remote --symref <frontend-remote> HEAD
  git ls-remote --heads <frontend-remote>
  ```

  Expected: a reachable default branch and its current commit. Record the branch name and commit before making any change.

- [x] **Step 2: Record the approved two-repository ownership decision.**

  ADR-002 supersedes the single-repository layout and local-composition assumptions. Backend Compose contains FastAPI, PostgreSQL, Redis, and backend-owned supporting infrastructure only. Frontend development runs Vite from `apexscan-frontend`; neither repository uses sibling-repository paths, and neither CI workflow clones the other repository to run quality gates.

## Task 1: Baseline and secret-safety evidence

**Files:**
- Inspect: both repository roots, their `.gitignore` files, `.env.example` files, CI workflows, Docker files, and tracked-file lists.
- Test: Git status, tracked-file secret-name scan, ignore-rule verification.

- [ ] **Step 1: Record clean backend baseline.**

  Run:

  ```bash
  git -C <backend-repo> status --short --branch
  git -C <backend-repo> log --oneline --decorate -5
  git -C <backend-repo> remote -v
  ```

  Expected: no uncommitted implementation changes; the existing `main` and `phase-1-foundation-hardening` history remains intact.

- [ ] **Step 2: Record frontend baseline without altering it.**

  Clone the verified remote into a unique temporary directory, then run the same status, log, remote, and tracked-file checks. Confirm the existing root `.gitignore` and `LICENSE` are tracked before copying any application file.

- [ ] **Step 3: Perform filename-only secret checks in both repositories.**

  Run a tracked-file scan for common token/private-key patterns and confirm that only placeholder configuration is present in any `.env.example`. Do not print values. In the backend root `.gitignore`, add `credentials/`, `*.pem`, and `*.key` alongside the existing `.env`, dependency, build, cache, and editor-local rules. In the frontend root `.gitignore`, preserve existing rules and add the same credential/key rules if absent.

## Task 2: Import the frontend without history rewriting

**Files:**
- Copy from backend: `frontend/src/`, `frontend/public/`, `frontend/package.json`, `frontend/package-lock.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/eslint.config.js`, `frontend/index.html`, and `frontend/Dockerfile`.
- Preserve in frontend: existing `.gitignore`, existing `LICENSE`.
- Create in frontend: root `README.md`, `.github/workflows/ci.yml`, and a frontend-only `.env.example` for `VITE_API_BASE_URL` and `VITE_WS_BASE_URL`.
- Test: frontend dependency integrity, lint, type-check, build, audit.

- [ ] **Step 1: Create a frontend split branch from the frontend repository's current default branch.**

  ```bash
  git switch -c phase-1-frontend-separation
  ```

  Do not commit directly to the frontend default branch.

- [ ] **Step 2: Copy the application to the frontend repository root without copying Git metadata or generated output.**

  Copy only the listed tracked application/configuration files from `<backend-repo>/frontend/` to the frontend checkout root. Exclude `.git`, `.gitignore`, `node_modules`, `dist`, `.vite`, and `*.tsbuildinfo`. The resulting layout must be `src/`, `public/`, `package.json`, and Vite/TypeScript/ESLint configuration at the repository root—never `frontend/src/`.

- [ ] **Step 3: Merge ignore rules and preserve licensing.**

  Retain every existing frontend `.gitignore` line. Add the required frontend entries only when absent: `node_modules/`, `dist/`, `.vite/`, `*.tsbuildinfo`, `.env`, `.env.*`, `!.env.example`, `*.local`, `credentials/`, `*.pem`, and `*.key`. Confirm `LICENSE` has no diff; if an application LICENSE is discovered in the source, stop and report the conflict rather than replacing the existing frontend LICENSE.

- [ ] **Step 4: Add frontend-only setup and CI.**

  Write a frontend README that identifies `VITE_API_BASE_URL` and `VITE_WS_BASE_URL` as deployment/environment inputs, points architecture readers to the canonical backend documentation, and contains only frontend setup/validation commands. Add a frontend workflow using Node `22.22.0` that runs `npm ci`, `npm run lint`, `npm run typecheck`, configured tests if a test script exists, `npm run build`, and `npm audit --audit-level=high` from the repository root.

- [ ] **Step 5: Validate the imported frontend before touching the backend copy.**

  Run:

  ```bash
  npm ci
  npm run lint
  npm run typecheck
  npm run build
  npm audit --audit-level=high
  ```

  Expected: every configured command exits successfully, `npm audit` reports zero vulnerabilities, and `npm ls react-router react-router-dom --all` shows `react-router@8.3.0` with no `react-router-dom` dependency. If no test script exists, record frontend tests as `NOT APPLICABLE` rather than fabricating one.

- [ ] **Step 6: Commit and push the frontend import branch.**

  Commit only the imported application, merged ignore rules, frontend CI, and frontend setup documentation. Push normally, open a PR to the frontend default branch, and wait for the real frontend workflow. Do not merge until the workflow is green.

## Task 3: Remove frontend ownership from the backend repository

**Files:**
- Modify: `apexscan-backend/.github/workflows/ci.yml`, `README.md`, `.gitignore`, `.env.example`, `docker-compose.yml`, `scripts/dev.sh`, and the host-level Nginx deployment comments/configuration where required.
- Remove after frontend validation: `frontend/`.
- Retain: `backend/`, `backend/tests/`, `docs/`, database/Alembic files, `docker/postgres/`, backend Dockerfile, and backend-specific configuration.
- Test: backend CI YAML, backend quality gates where locally available, approved Compose topology validation.

- [ ] **Step 1: Remove frontend quality-gate ownership from backend CI.**

  Delete only the `frontend` job from the backend workflow. Keep the backend job on Python 3.13 and the infrastructure job. Change the infrastructure job to build and start the backend, PostgreSQL, and Redis only; it must neither build `./frontend` nor clone the frontend repository merely to run frontend checks.

- [ ] **Step 2: Apply the approved independent local-development model.**

  Follow ADR-002 exactly. Update `docker-compose.yml` and `scripts/dev.sh` so they start FastAPI, PostgreSQL, Redis, and backend-owned supporting infrastructure only. Split the current environment example into a backend-only contract and a frontend-only contract. Retain `docker/nginx/nginx.conf` in the backend repository as host-level deployment infrastructure, remove any false local-Compose activation instruction, and do not duplicate it in the frontend repository. Preserve PostgreSQL, Redis, backend migrations, backend health validation, and clean shutdown.

- [ ] **Step 3: Correct backend-owned documentation only.**

  Update the backend README and other non-frozen implementation/setup documents to remove frontend setup and frontend quality commands. Do not copy canonical architecture documents into the frontend repository or edit frozen architecture documents without the approved amendment.

- [ ] **Step 4: Remove the backend `frontend/` directory only after frontend evidence exists.**

  Confirm the frontend import branch is pushed and its locally available checks passed. Then use `git rm -r frontend` from the backend repository and verify no reference remains to the deleted path. The host-level Nginx configuration may retain service-DNS routing for deployment, but it must not have a frontend build context, bind mount, or sibling-path dependency.

- [ ] **Step 5: Commit and push the backend split branch.**

  Commit the CI split, approved orchestration changes, backend documentation corrections, and the `frontend/` removal to `phase-1-foundation-hardening`. Push normally; do not rewrite `main`.

## Task 4: Real CI and Git validation

**Files:**
- Test: both GitHub Actions workflows, both repository branch/remote/status reports.

- [ ] **Step 1: Verify frontend PR CI.**

  Confirm its real GitHub Actions run shows Node `22.22.0`, `npm ci`, ESLint, TypeScript, configured tests if any, production build, and an npm audit result of zero vulnerabilities.

- [ ] **Step 2: Verify backend PR CI.**

  Confirm its real run shows Python `3.13`, dependency installation, Ruff format, Ruff lint, mypy, pytest, pip-audit, and the approved Docker/Compose runtime checks. Investigate and fix only demonstrated Phase 1 failures.

- [ ] **Step 3: Record each repository's final Git state.**

  For both repositories, record branch, remote, clean/dirty worktree state, commits created, changed files, pushed status, PR URL/status, and workflow results. Verify no force push or history rewrite occurred.

## Task 5: Completion report

**Files:**
- Report: repository split report and Phase 1 status only.

- [ ] **Step 1: Produce the required split matrix.**

  Report backend/frontend final structures, moved and removed files, preserved frontend files, ignore merge result, LICENSE status, CI split, Compose status, validation evidence, security result, Git state for both repositories, architecture deviations, technical debt, and remaining Phase 1 blockers.

- [ ] **Step 2: State final statuses from evidence.**

  Declare `REPOSITORY SPLIT — PASS` only after both repositories are safely separated and their required validations have evidence. Declare `PHASE 1 — PASS` only after both real workflows and the required PR flows are green; otherwise report the precise blocker.
