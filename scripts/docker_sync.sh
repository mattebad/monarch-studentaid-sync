#!/usr/bin/env bash
set -euo pipefail

# Run the sync via docker compose (intended for Docker Desktop / Unraid scheduling).
#
# Usage examples:
#   ./scripts/docker_sync.sh setup-accounts
#   ./scripts/docker_sync.sh preflight
#   ./scripts/docker_sync.sh dry-run --payments-since 2025-01-01
#   ./scripts/docker_sync.sh run --payments-since 2025-01-01
#   ./scripts/docker_sync.sh update
#   ./scripts/docker_sync.sh update-run --payments-since 2025-01-01
#   ./scripts/docker_sync.sh update-dry-run --payments-since 2025-01-01

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

MODE="${1:-run}"
shift || true

SERVICE="studentaid-monarch-sync"

mkdir -p data

_git_pull_ff_only() {
  if command -v git >/dev/null 2>&1 && [ -d ".git" ]; then
    # Best-effort update; keep it safe/non-destructive.
    git pull --ff-only
  fi
}

_compose_build() {
  # Pull newer base image layers when available.
  # Set NO_CACHE=1 to force a full rebuild.
  local args=(build --pull)
  if [ "${NO_CACHE:-}" = "1" ]; then
    args+=(--no-cache)
  fi
  args+=("${SERVICE}")
  docker compose "${args[@]}"
}

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
  update)
    _git_pull_ff_only
    _compose_build
    ;;
  update-run)
    _git_pull_ff_only
    _compose_build
    docker compose run --rm "${SERVICE}" sync "$@"
    ;;
  update-dry-run)
    _git_pull_ff_only
    _compose_build
    docker compose run --rm "${SERVICE}" sync --dry-run "$@"
    ;;
  *)
    echo "Unknown mode: ${MODE}"
    echo "Expected: setup-accounts | preflight | dry-run | run | update | update-run | update-dry-run"
    exit 2
    ;;
esac


