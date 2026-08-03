# ApexScan

Professional trading scanner platform — **Phase 1: infrastructure skeleton**.

ApexScan is designed from day one to scale to **100+ strategies** across
**multiple brokers** (Dhan, Binance, Zerodha, …). This repository currently
contains only the production-grade project skeleton: folder structure,
configuration, database/cache wiring, a FastAPI starter, and a React shell.

> No trading strategy, indicator, or market logic is implemented yet.
> This phase is purely infrastructure and architecture.

---

## Tech stack

| Layer          | Technology                                                        |
| -------------- | ----------------------------------------------------------------- |
| Backend        | Python 3.13+, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, asyncio, uvicorn |
| Data stores    | PostgreSQL, Redis                                                  |
| Frontend       | React 19, TypeScript, Vite, Tailwind CSS, Zustand, TanStack Query, React Router, AG Grid, TradingView Lightweight Charts |
| Infrastructure | Docker, Docker Compose, Nginx (prepared)                          |

---

## Repository layout

```
ApexScan/
├── backend/        FastAPI application (clean architecture)
├── frontend/       React + Vite single-page app
├── docs/           Architecture & design documentation
├── docker/         Dockerfiles and service configs (nginx, postgres)
├── scripts/        Developer / operational helper scripts
├── tests/          Cross-cutting / end-to-end test suites
├── docker-compose.yml
├── .env.example
└── README.md
```

See [`docs/00_PROJECT_OVERVIEW.md`](docs/00_PROJECT_OVERVIEW.md) for the full
documentation index.

---

## Quick start (Docker)

```bash
cp .env.example .env          # then edit secrets
docker compose up --build
```

- Backend API  → http://localhost:8000
- API docs      → http://localhost:8000/docs
- Frontend      → http://localhost:5173

## Quick start (local backend)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

## Quick start (local frontend)

```bash
cd frontend
npm install
npm run dev
```

## Quality gates

Phase 1 requires the following checks before a change is reviewed. CI runs the
same commands for every push and pull request.

```bash
# Backend (Python 3.13)
cd backend
python -m pip install -e ".[dev]"
python -m ruff format --check .
python -m ruff check .
python -m mypy app
python -m pytest
python -m pip_audit

# Frontend (Node.js 22.22+)
cd ../frontend
npm ci
npm run lint
npm run typecheck
npm run build
npm audit --audit-level=high

# From the repository root: validate the local stack definition, then run it.
docker compose config --quiet
docker compose up --build
```

Docker Compose requires a local `.env`; create one from `.env.example` before
starting the stack.

### Docker runtime validation

Run the following on a Docker-capable machine to complete the Phase 1 runtime
gate. Do not use `down -v`; database and Redis volumes must be preserved while
validating shutdown.

```bash
# From the repository root
docker compose pull postgres redis
docker compose build backend frontend
docker compose up -d postgres redis
# Wait until both dependency health checks pass (up to 60 seconds).
for attempt in {1..30}; do
  postgres_status="$(docker inspect --format '{{.State.Health.Status}}' apexscan-postgres)"
  redis_status="$(docker inspect --format '{{.State.Health.Status}}' apexscan-redis)"
  [[ "$postgres_status" == healthy && "$redis_status" == healthy ]] && break
  sleep 2
done
[[ "$postgres_status" == healthy && "$redis_status" == healthy ]]
docker compose run --rm backend alembic upgrade head
docker compose up -d backend frontend
# Wait for the backend liveness probe (expected response: {"status":"ok"}).
for attempt in {1..30}; do
  response="$(curl --fail --silent --show-error http://localhost:8000/api/v1/health || true)"
  [[ "$response" == '{"status":"ok"}' ]] && break
  sleep 2
done
[[ "$response" == '{"status":"ok"}' ]]
docker compose ps
docker compose logs --no-color postgres redis backend frontend
docker compose down
test -z "$(docker compose ps --status running --services)"
```

Capture the readiness result, migration output, health response, service logs,
and clean-shutdown result as acceptance evidence.

---

## Architecture principles

Clean Architecture · Repository Pattern · Service Pattern · Dependency
Injection · Async-first · SOLID · No global state · Fully typed.

Each broker is an **adapter** behind a common interface; each strategy is a
plug-in registered with the **strategy manager**. This keeps the core engine
agnostic to any specific broker or strategy.
