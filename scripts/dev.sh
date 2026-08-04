#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ApexScan — developer convenience script.
# Brings up the backend/infrastructure stack via Docker Compose with a fresh
# build. The React application runs separately from apexscan-frontend.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo "No .env found — copying from .env.example"
  cp .env.example .env
fi

docker compose up --build "$@"
