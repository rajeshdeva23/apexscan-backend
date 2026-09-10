#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ApexScan — production host update reference (DEPLOY-3B).
#
# The authoritative deploy mechanism is `deploy/transport.py` (run by the
# pipeline over hardened SSH). This script documents the equivalent MINIMUM
# host-side operation for an operator: move production from one immutable,
# digest-pinned backend image to another using the single production Compose
# authority. It never builds from source, never runs migrations, and touches
# only the backend service (`--no-deps`) so Postgres/Redis and their volumes are
# undisturbed. Project identity is pinned (`-p apexscan`) so the deploy attaches
# to the existing project's volumes/networks.
#
# The backend restart it performs re-runs application startup, which — when
# market_provider_enabled=true — authenticates Dhan; only run after Dhan restart
# safety is confirmed.
#
# Required environment:
#   APEXSCAN_IMAGE   digest-pinned image, e.g.
#                    ghcr.io/rajeshdeva23/apexscan-backend@sha256:<digest>
#   RELEASE_DIR      versioned release dir holding docker-compose.production.yml
#                    (e.g. /opt/apexscan/releases/<sha>)
#
# On the production host Docker requires sudo; prefix the docker commands with
# `sudo -n` there. Not executed during DEPLOY-3B (transport unprovisioned).
# ---------------------------------------------------------------------------
set -euo pipefail

: "${APEXSCAN_IMAGE:?APEXSCAN_IMAGE must be set to a digest-pinned image reference}"
: "${RELEASE_DIR:?RELEASE_DIR must point at the versioned release directory}"

COMPOSE=(docker compose -p apexscan -f "${RELEASE_DIR}/docker-compose.production.yml"
  --project-directory "${RELEASE_DIR}")

echo "Pulling immutable backend image: ${APEXSCAN_IMAGE}"
APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${COMPOSE[@]}" pull backend

echo "Updating backend only (no deps, no build, no migrations)"
APEXSCAN_IMAGE="${APEXSCAN_IMAGE}" "${COMPOSE[@]}" up -d --no-deps --no-build backend

echo "Backend updated to ${APEXSCAN_IMAGE}"
