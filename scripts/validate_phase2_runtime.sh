#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ApexScan — Phase 2 Compose runtime acceptance validation.
#
# Requires a running backend-owned Compose stack. It validates startup,
# liveness, readiness, dependency outage/recovery, and process continuity.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly API_BASE_URL="${APEXSCAN_API_BASE_URL:-http://localhost:8000/api/v1}"
readonly MAX_ATTEMPTS=30
readonly POLL_INTERVAL_SECONDS=2
readonly CURL_CONNECT_TIMEOUT_SECONDS=2
readonly CURL_MAX_TIME_SECONDS=5

readonly LIVE_RESPONSE='{"status":"live"}'
readonly STARTED_RESPONSE='{"status":"started"}'
readonly READY_RESPONSE='{"status":"ready","startup":"started","dependencies":{"database":"healthy","redis":"healthy"}}'
readonly POSTGRES_UNAVAILABLE_RESPONSE='{"status":"not_ready","startup":"started","dependencies":{"database":"unhealthy","redis":"healthy"}}'
readonly REDIS_UNAVAILABLE_RESPONSE='{"status":"not_ready","startup":"started","dependencies":{"database":"healthy","redis":"unhealthy"}}'

cd "$ROOT_DIR"

show_diagnostics() {
  docker compose ps || true
  docker compose logs --no-color backend postgres redis || true
}

trap show_diagnostics ERR

wait_for_response() {
  local path="$1"
  local expected_status="$2"
  local expected_body="$3"
  local attempt response body status

  for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    response="$(
      curl \
        --silent \
        --connect-timeout "$CURL_CONNECT_TIMEOUT_SECONDS" \
        --max-time "$CURL_MAX_TIME_SECONDS" \
        --write-out $'\n%{http_code}' \
        "${API_BASE_URL}${path}" \
        2>/dev/null || true
    )"
    body="${response%$'\n'*}"
    status="${response##*$'\n'}"
    if [[ "$status" == "$expected_status" && "$body" == "$expected_body" ]]; then
      printf 'Validated %s: HTTP %s\n' "$path" "$expected_status"
      return
    fi
    sleep "$POLL_INTERVAL_SECONDS"
  done

  printf 'Expected %s to return HTTP %s with %s\n' "$path" "$expected_status" "$expected_body" >&2
  return 1
}

wait_for_service_health() {
  local service="$1"
  local attempt container_id health_status

  for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    container_id="$(docker compose ps -q "$service")"
    if [[ -n "$container_id" ]]; then
      health_status="$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
      if [[ "$health_status" == "healthy" ]]; then
        printf 'Validated %s Compose health\n' "$service"
        return
      fi
    fi
    sleep "$POLL_INTERVAL_SECONDS"
  done

  printf '%s did not become healthy within the configured timeout\n' "$service" >&2
  return 1
}

backend_restart_count() {
  local container_id
  container_id="$(docker compose ps -q backend)"
  [[ -n "$container_id" ]]
  docker inspect --format '{{.RestartCount}}' "$container_id"
}

assert_backend_continues_running() {
  local expected_restart_count="$1"
  local container_id running_state actual_restart_count

  container_id="$(docker compose ps -q backend)"
  [[ -n "$container_id" ]]
  running_state="$(docker inspect --format '{{.State.Running}}' "$container_id")"
  actual_restart_count="$(docker inspect --format '{{.RestartCount}}' "$container_id")"
  [[ "$running_state" == "true" ]]
  [[ "$actual_restart_count" == "$expected_restart_count" ]]
}

wait_for_response "/health/startup" "200" "$STARTED_RESPONSE"
wait_for_response "/health/ready" "200" "$READY_RESPONSE"
wait_for_response "/health" "200" "$LIVE_RESPONSE"

initial_restart_count="$(backend_restart_count)"

docker compose stop postgres
wait_for_response "/health/ready" "503" "$POSTGRES_UNAVAILABLE_RESPONSE"
wait_for_response "/health" "200" "$LIVE_RESPONSE"
assert_backend_continues_running "$initial_restart_count"

docker compose start postgres
wait_for_service_health postgres
wait_for_response "/health/ready" "200" "$READY_RESPONSE"

docker compose stop redis
wait_for_response "/health/ready" "503" "$REDIS_UNAVAILABLE_RESPONSE"
wait_for_response "/health" "200" "$LIVE_RESPONSE"
assert_backend_continues_running "$initial_restart_count"

docker compose start redis
wait_for_service_health redis
wait_for_response "/health/ready" "200" "$READY_RESPONSE"

printf 'Phase 2 Compose runtime acceptance validation passed.\n'
