#!/usr/bin/env bash
set -euo pipefail

# Run the sync via docker compose (intended for Docker Desktop / Unraid scheduling).
#
# Usage examples:
#   ./scripts/docker_sync.sh setup-accounts
#   ./scripts/docker_sync.sh preflight
#   ./scripts/docker_sync.sh dry-run --payments-since 2025-01-01
#   ./scripts/docker_sync.sh run --payments-since 2025-01-01

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

MODE="${1:-run}"
shift || true

SERVICE="studentaid-monarch-sync"

mkdir -p data

case "${MODE}" in
  setup-accounts)
    docker compose run --rm --build "${SERVICE}" setup-monarch-accounts --apply "$@"
    ;;
  preflight)
    docker compose run --rm --build "${SERVICE}" preflight "$@"
    ;;
  dry-run)
    docker compose run --rm "${SERVICE}" sync --dry-run "$@"
    ;;
  run)
    docker compose run --rm "${SERVICE}" sync "$@"
    ;;
  *)
    echo "Unknown mode: ${MODE}"
    echo "Expected: setup-accounts | preflight | dry-run | run"
    exit 2
    ;;
esac


