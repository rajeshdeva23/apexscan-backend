#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ApexScan — production host update reference (DEPLOY-3B; two-service since H9C-P2).
#
# The authoritative deploy mechanism is `deploy/transport.py` (run by the
# pipeline over hardened SSH). This script documents the equivalent MINIMUM
# host-side operation for an operator: move production from one immutable,
# digest-pinned image to another using the single production Compose authority.
# It never builds from source, never runs migrations, and touches only the
# selected application service(s) (`--no-deps`) so Postgres/Redis and their
# volumes are undisturbed. Project identity is pinned (`-p apexscan`).
#
# DEPLOYMENT != ACTIVATION. This script only moves image revisions. It NEVER sets
# ownership or IPC-authority flags — those live in the external env files and are
# turned on only by a separate governed H9C operation. Deploying the
# market-ingestion service with the flags off starts an inert, idle container.
#
# The backend restart re-runs application startup, which — when
# market_provider_enabled=true — authenticates Dhan; only run after Dhan restart
# safety is confirmed (Gate G/H). The same applies to activating ingestion.
#
# Usage:
#   remote_update.sh [backend|market-ingestion|both]   (default: backend)
#
# Both application services run the SAME ${APEXSCAN_IMAGE}. Use `both` for a
# decoupling-compatible release so an operator does not accidentally update only
# one service and leave the two on different revisions.
#
# Required environment:
#   APEXSCAN_IMAGE   digest-pinned image, e.g.
#                    ghcr.io/rajeshdeva23/apexscan-backend@sha256:<digest>
#   RELEASE_DIR      versioned release dir holding docker-compose.production.yml
#
# On the production host Docker requires sudo; prefix docker commands with
# `sudo -n` there. Not executed during offline phases (transport unprovisioned).
# ---------------------------------------------------------------------------
set -euo pipefail

: "${APEXSCAN_IMAGE:?APEXSCAN_IMAGE must be set to a digest-pinned image reference}"
: "${RELEASE_DIR:?RELEASE_DIR must point at the versioned release directory}"

TARGET="${1:-backend}"

COMPOSE=(docker compose -p apexscan -f "${RELEASE_DIR}/docker-compose.production.yml"
  --project-directory "${RELEASE_DIR}")

# market-ingestion is profile-gated; it is only ever addressed with its profile.
INGESTION_COMPOSE=("${COMPOSE[@]}" --profile market-ingestion)

update_backend() {
  echo "Pulling immutable image for backend: ${APEXSCAN_IMAGE}"
  APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${COMPOSE[@]}" pull backend
  echo "Updating backend only (no deps, no build, no migrations)"
  APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${COMPOSE[@]}" up -d --no-deps --no-build backend
  echo "backend updated to ${APEXSCAN_IMAGE}"
}

update_ingestion() {
  echo "Pulling immutable image for market-ingestion: ${APEXSCAN_IMAGE}"
  APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${INGESTION_COMPOSE[@]}" pull market-ingestion
  echo "Updating market-ingestion only (no deps, no build; flags unchanged/inert)"
  APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${INGESTION_COMPOSE[@]}" \
    up -d --no-deps --no-build market-ingestion
  echo "market-ingestion updated to ${APEXSCAN_IMAGE}"
}

case "${TARGET}" in
  backend)
    update_backend
    ;;
  market-ingestion)
    update_ingestion
    ;;
  both)
    # Same immutable ${APEXSCAN_IMAGE} to both application services in one release.
    update_backend
    update_ingestion
    echo "both services updated to the SAME image: ${APEXSCAN_IMAGE}"
    ;;
  *)
    echo "invalid target '${TARGET}' (expected: backend | market-ingestion | both)" >&2
    exit 2
    ;;
esac
