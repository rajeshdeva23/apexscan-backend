#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ApexScan — production host update (DEPLOY-1).
#
# The MINIMUM operation to move production from one immutable, digest-pinned
# backend image to another. Runs ON THE PRODUCTION HOST, invoked by the deploy
# pipeline over the configured transport. It never builds from source, never
# runs `docker compose up --build`, never runs database migrations, and touches
# only the backend service so Postgres/Redis and their volumes are undisturbed.
#
# The backend restart it performs re-runs application startup, which — when
# market_provider_enabled=true — authenticates Dhan. The pipeline must only
# invoke this after the operator has confirmed Dhan restart safety (§11).
#
# Required environment:
#   APEXSCAN_IMAGE  digest-pinned image, e.g.
#                   ghcr.io/rajeshdeva23/apexscan-backend@sha256:<digest>
#
# Not executed during DEPLOY-1 (no production transport is provisioned).
# ---------------------------------------------------------------------------
set -euo pipefail

: "${APEXSCAN_IMAGE:?APEXSCAN_IMAGE must be set to a digest-pinned image reference}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

echo "Pulling immutable backend image: ${APEXSCAN_IMAGE}"
APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${COMPOSE[@]}" pull backend

echo "Recreating backend from the pulled image (no build, no migrations)"
APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${COMPOSE[@]}" up -d --no-build backend

echo "Backend updated to ${APEXSCAN_IMAGE}"
