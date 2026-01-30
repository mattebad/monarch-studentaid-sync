from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def _get_env_file(provider: str) -> Optional[Path]:
    key = f"PORTAL_ENV_FILE_{provider.upper()}"
    env_path = os.getenv(key)
    if env_path:
        return Path(env_path)

    fallback = os.getenv("PORTAL_ENV_FILE")
    if fallback:
        return Path(fallback)

    default = ROOT / "portal.env"
    if default.exists():
        return default

    return None


def _build_env(provider: str, *, base_env: dict[str, str]) -> dict[str, str]:
    env = base_env.copy()
    env["SERVICER_PROVIDER"] = provider

    base_override = env.get(f"PORTAL_BASE_URL_{provider.upper()}")
    if base_override:
        env["SERVICER_BASE_URL"] = base_override
    else:
        env["SERVICER_BASE_URL"] = f"https://{provider}.studentaid.gov"

    user_override = env.get(f"PORTAL_USERNAME_{provider.upper()}")
    pass_override = env.get(f"PORTAL_PASSWORD_{provider.upper()}")
    if user_override:
        env["SERVICER_USERNAME"] = user_override
    if pass_override:
        env["SERVICER_PASSWORD"] = pass_override

    groups_override = env.get(f"PORTAL_LOAN_GROUPS_{provider.upper()}")
    if groups_override:
        env["LOAN_GROUPS"] = groups_override

    return env


def _skip_or_fail(reason: str) -> None:
    # Portal smoke tests require real credentials and should not fail local unit test runs by default.
    # To force failures locally (e.g., in a dedicated integration run), set REQUIRE_PORTAL_TESTS=1.
    if os.getenv("REQUIRE_PORTAL_TESTS") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def _skip_if_missing(provider: str, *, env: dict[str, str], env_file: Optional[Path]) -> None:
    if env_file is not None and not env_file.exists():
        _skip_or_fail(f"Env file not found for {provider}: {env_file}")

    if not env.get("SERVICER_USERNAME") or not env.get("SERVICER_PASSWORD"):
        _skip_or_fail(f"Missing SERVICER_USERNAME/SERVICER_PASSWORD for {provider}.")

    if not env.get("LOAN_GROUPS"):
        _skip_or_fail(f"Missing LOAN_GROUPS for {provider}.")

    if not env.get("MONARCH_TOKEN") and not (env.get("MONARCH_EMAIL") and env.get("MONARCH_PASSWORD")):
        _skip_or_fail("Missing Monarch auth (MONARCH_TOKEN or MONARCH_EMAIL + MONARCH_PASSWORD).")

    if os.getenv("PORTAL_SMOKE_SKIP_IMAP") != "1":
        if not env.get("GMAIL_IMAP_USER") or not env.get("GMAIL_IMAP_APP_PASSWORD"):
            _skip_or_fail("Missing Gmail IMAP creds (set GMAIL_IMAP_USER + GMAIL_IMAP_APP_PASSWORD).")


def _run_cmd(args: list[str], *, env: dict[str, str]) -> None:
    timeout = int(os.getenv("PORTAL_SMOKE_TIMEOUT", "1200"))
    subprocess.run(args, cwd=ROOT, env=env, check=True, timeout=timeout)


def _run_preflight_and_dry_run(provider: str) -> None:
    env_file = _get_env_file(provider)
    base_env = os.environ.copy()
    if env_file is not None and env_file.exists():
        values = dotenv_values(env_file)
        for key, value in values.items():
            if value is None or key in base_env:
                continue
            base_env[key] = value

    env = _build_env(provider, base_env=base_env)
    _skip_if_missing(provider, env=env, env_file=env_file)

    cmd_base = [sys.executable, "-m", "studentaid_monarch_sync"]
    if env_file:
        cmd_base += ["--env-file", str(env_file)]

    preflight_cmd = cmd_base + ["preflight"]
    if os.getenv("PORTAL_SMOKE_SKIP_IMAP") == "1":
        preflight_cmd.append("--skip-imap")

    dry_run_cmd = cmd_base + ["sync", "--dry-run"]
    if provider == "nelnet":
        # Some Nelnet accounts show no loan-group details (e.g. closed/transferred loans).
        # We still want to exercise login + payment allocation parsing.
        dry_run_cmd.append("--skip-loans")
    max_payments = os.getenv("PORTAL_SMOKE_MAX_PAYMENTS")
    if max_payments:
        dry_run_cmd += ["--max-payments", max_payments]
    payments_since = os.getenv("PORTAL_SMOKE_PAYMENTS_SINCE")
    if payments_since:
        dry_run_cmd += ["--payments-since", payments_since]

    _run_cmd(preflight_cmd, env=env)
    _run_cmd(dry_run_cmd, env=env)


@pytest.mark.portal
def test_cri_preflight_and_dry_run() -> None:
    _run_preflight_and_dry_run("cri")


@pytest.mark.portal
def test_nelnet_preflight_and_dry_run() -> None:
    _run_preflight_and_dry_run("nelnet")
