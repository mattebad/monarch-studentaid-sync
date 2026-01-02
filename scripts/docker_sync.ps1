Param(
  [Parameter(Position = 0)]
  [ValidateSet("setup-accounts", "preflight", "dry-run", "run")]
  [string]$Mode = "run",

  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Args
)

$ErrorActionPreference = "Stop"

# Run the sync via docker compose (intended for Docker Desktop scheduling on Windows).
#
# Usage examples:
#   pwsh -File .\scripts\docker_sync.ps1 setup-accounts
#   pwsh -File .\scripts\docker_sync.ps1 preflight
#   pwsh -File .\scripts\docker_sync.ps1 dry-run --payments-since 2025-01-01
#   pwsh -File .\scripts\docker_sync.ps1 run --payments-since 2025-01-01

$RepoDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoDir

$Service = "studentaid-monarch-sync"

New-Item -ItemType Directory -Force -Path (Join-Path $RepoDir "data") | Out-Null

switch ($Mode) {
  "setup-accounts" {
    docker compose run --rm --build $Service setup-monarch-accounts --apply @Args
  }
  "preflight" {
    docker compose run --rm --build $Service preflight @Args
  }
  "dry-run" {
    docker compose run --rm $Service sync --dry-run @Args
  }
  "run" {
    docker compose run --rm $Service sync @Args
  }
}


