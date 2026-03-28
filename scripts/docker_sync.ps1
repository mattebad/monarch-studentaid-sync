Param(
  [Parameter(Position = 0)]
  [ValidateSet("setup-accounts", "preflight", "dry-run", "run", "update", "update-run", "update-dry-run")]
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

function Invoke-GitPull {
  if (Get-Command git -ErrorAction SilentlyContinue) {
    if (Test-Path (Join-Path $RepoDir ".git")) {
      # Best-effort update; keep it safe/non-destructive.
      git pull --ff-only
    }
  }
}

function Invoke-ComposeBuild {
  $buildArgs = @("build", "--pull")
  if ($env:NO_CACHE -eq "1") {
    $buildArgs += "--no-cache"
  }
  $buildArgs += $Service
  docker compose @buildArgs
}

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
  "update" {
    Invoke-GitPull
    Invoke-ComposeBuild
  }
  "update-run" {
    Invoke-GitPull
    Invoke-ComposeBuild
    docker compose run --rm $Service sync @Args
  }
  "update-dry-run" {
    Invoke-GitPull
    Invoke-ComposeBuild
    docker compose run --rm $Service sync --dry-run @Args
  }
}


