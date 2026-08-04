# ApexScan Backend

Backend and infrastructure repository for ApexScan — **Phase 1: foundation**.

ApexScan is designed from day one to scale to **100+ strategies** across
**multiple brokers** (Dhan, Binance, Zerodha, …). This repository currently
contains only the production-grade project skeleton: folder structure,
configuration, database/cache wiring, and a FastAPI starter.

> No trading strategy, indicator, or market logic is implemented yet.
> This phase is purely infrastructure and architecture.

---

## Tech stack

| Layer          | Technology                                                        |
| -------------- | ----------------------------------------------------------------- |
| Backend        | Python 3.13+, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, asyncio, uvicorn |
| Data stores    | PostgreSQL, Redis                                                  |
| Infrastructure | Docker, Docker Compose, Nginx (prepared)                          |

---

## Repository layout

```
ApexScan Backend/
├── backend/        FastAPI application (clean architecture)
├── docs/           Architecture & design documentation
├── docker/         Dockerfiles and service configs (nginx, postgres)
├── scripts/        Developer / operational helper scripts
├── tests/          Cross-cutting backend test suites
├── docker-compose.yml
├── .env.example
└── README.md
```

See [`docs/00_PROJECT_OVERVIEW.md`](docs/00_PROJECT_OVERVIEW.md) for the full
documentation index.

The React application, its Node.js tooling, and frontend quality gates live in
the separate [apexscan-frontend](https://github.com/rajeshdeva23/apexscan-frontend)
repository. It communicates with this backend through configured HTTP API and
WebSocket URLs.

---

## Quick start (Docker)

```bash
cp .env.example .env          # then edit secrets
docker compose up --build
```

- Backend API  → http://localhost:8000
- API docs      → http://localhost:8000/docs

## Quick start (local backend)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
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

# From the repository root: validate the local stack definition, then run it.
docker compose config --quiet
docker compose up --build
```

Docker Compose requires a local `.env`; create one from `.env.example` before
starting the stack.

### Docker runtime validation

Run the following on a Docker-capable machine to validate Phase 2 runtime
health behavior. Do not use `down -v`; database and Redis volumes must be
preserved while validating shutdown.

```bash
# From the repository root
docker compose pull postgres redis
docker compose build backend
docker compose up -d postgres redis
# Wait for both dependency health checks before running migrations.
for attempt in {1..30}; do
  postgres_status="$(docker inspect --format '{{.State.Health.Status}}' apexscan-postgres)"
  redis_status="$(docker inspect --format '{{.State.Health.Status}}' apexscan-redis)"
  [[ "$postgres_status" == healthy && "$redis_status" == healthy ]] && break
  sleep 2
done
[[ "$postgres_status" == healthy && "$redis_status" == healthy ]]
docker compose run --rm backend alembic upgrade head
docker compose up -d backend
# Validates startup/readiness, PostgreSQL and Redis outage/recovery,
# liveness continuity, and backend process continuity.
bash scripts/validate_phase2_runtime.sh
docker compose down
test -z "$(docker compose ps --status running --services)"
```

Capture the script output, migration output, service logs, and clean-shutdown
result as acceptance evidence.

---

## Architecture principles

Clean Architecture · Repository Pattern · Service Pattern · Dependency
Injection · Async-first · SOLID · No global state · Fully typed.

Each broker is an **adapter** behind a common interface; each strategy is a
plug-in registered with the **strategy manager**. This keeps the core engine
agnostic to any specific broker or strategy.
