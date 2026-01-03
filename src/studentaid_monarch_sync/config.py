from __future__ import annotations

import os
import re
import json
from pathlib import Path
from typing import Literal
from typing import Union
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_LOAN_GROUP_RE = re.compile(r"^[A-Z0-9]{2,8}$")


def _expand_env_vars(value: object) -> object:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            return os.getenv(var, "")

        return _ENV_VAR_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    return value


def _derive_provider_from_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    # e.g. "nelnet.studentaid.gov" -> "nelnet"
    if "." in host:
        return host.split(".", 1)[0]
    return host


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "t", "yes", "y", "on"}


def _deep_merge(base: object, override: object) -> object:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    return override


def _parse_loan_groups_env(value: str) -> list[str]:
    s = (value or "").strip()
    if not s:
        return []

    # Support JSON list syntax for power users: ["AA","AB"]
    if s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                items = [str(x) for x in data]
            else:
                items = [s]
        except Exception:
            items = [s]
    else:
        # Comma and/or whitespace separated
        items = re.split(r"[,\s]+", s)

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        g = (item or "").strip().upper()
        if not g:
            continue
        if not _LOAN_GROUP_RE.match(g):
            # Keep it permissive but predictable: skip junk tokens rather than crashing.
            continue
        if g in seen:
            continue
        out.append(g)
        seen.add(g)
    return out


def _default_config_from_env() -> dict:
    """
    Provide a sensible env-only config so most users only need `.env`.

    This is intentionally redundant with `config.example.yaml`; YAML remains an optional advanced override.
    """
    return {
        "servicer": {
            "provider": os.getenv("SERVICER_PROVIDER", ""),
            "base_url": os.getenv("SERVICER_BASE_URL", ""),
            "username": os.getenv("SERVICER_USERNAME", ""),
            "password": os.getenv("SERVICER_PASSWORD", ""),
            "mfa_method": os.getenv("SERVICER_MFA_METHOD", "email"),
        },
        "gmail_imap": {
            "user": os.getenv("GMAIL_IMAP_USER", ""),
            "app_password": os.getenv("GMAIL_IMAP_APP_PASSWORD", ""),
            "folder": os.getenv("GMAIL_IMAP_FOLDER", "INBOX"),
            "sender_hint": os.getenv("GMAIL_IMAP_SENDER_HINT", ""),
            "subject_hint": os.getenv("GMAIL_IMAP_SUBJECT_HINT", ""),
            "code_regex": os.getenv("GMAIL_IMAP_CODE_REGEX", r"\b(\d{6})\b"),
        },
        "monarch": {
            "email": os.getenv("MONARCH_EMAIL", ""),
            "password": os.getenv("MONARCH_PASSWORD", ""),
            "token": os.getenv("MONARCH_TOKEN", ""),
            "mfa_secret": os.getenv("MONARCH_MFA_SECRET", ""),
            "transfer_category_name": os.getenv("MONARCH_TRANSFER_CATEGORY_NAME", "Transfer"),
            "payment_merchant_name": os.getenv("MONARCH_PAYMENT_MERCHANT_NAME", "Student Loan Payment"),
            "session_file": os.getenv("MONARCH_SESSION_FILE", "data/monarch_session.pickle"),
            "loan_account_name_template": os.getenv("MONARCH_LOAN_ACCOUNT_NAME_TEMPLATE", "{provider}-{group}"),
            "auto_create_loan_accounts": _env_bool("MONARCH_AUTO_CREATE_LOAN_ACCOUNTS", default=False),
        },
        "state": {
            "db_path": os.getenv("STATE_DB_PATH", "data/state.db"),
        },
        "logging": {
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "file_path": os.getenv("LOG_FILE", "data/sync.log"),
        },
    }


class ServicerConfig(BaseModel):
    """
    Configuration for a StudentAid servicer portal.

    Many federal loan servicers use a `https://{provider}.studentaid.gov` subdomain (e.g. `nelnet`, `mohela`).
    If yours differs, set `base_url` explicitly.
    """

    provider: str = ""
    base_url: str = ""
    username: str
    password: str
    mfa_method: Literal["email"] = "email"

    @model_validator(mode="after")
    def _fill_defaults_and_validate(self) -> "ServicerConfig":
        provider = (self.provider or "").strip().lower()
        base_url = (self.base_url or "").strip()

        # If provider is missing but base_url is present, derive it from host.
        if not provider and base_url:
            provider = _derive_provider_from_base_url(base_url)

        if not provider:
            raise ValueError("servicer.provider is required (e.g. 'nelnet', 'mohela', 'aidvantage')")
        if not _PROVIDER_SLUG_RE.match(provider):
            raise ValueError(
                "servicer.provider must be a slug like 'nelnet' (lowercase letters, numbers, hyphen only)"
            )

        if not base_url:
            base_url = f"https://{provider}.studentaid.gov"

        # Normalize base_url so other components can depend on it.
        base_url = base_url.rstrip("/")
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"servicer.base_url must be a full URL like 'https://{provider}.studentaid.gov'")

        self.provider = provider
        self.base_url = base_url
        return self


class GmailImapConfig(BaseModel):
    user: str
    app_password: str = Field(repr=False)
    folder: str = "INBOX"
    sender_hint: str = ""
    subject_hint: str = ""
    code_regex: str = r"\b(\d{6})\b"


class MonarchConfig(BaseModel):
    # If you use "Sign in with Apple", prefer token-based auth.
    token: str = Field(default="", repr=False)

    # Email/password auth (optional if token is set)
    email: str = ""
    password: str = Field(default="", repr=False)
    mfa_secret: str = Field(default="", repr=False)
    transfer_category_name: str = "Transfer"
    payment_merchant_name: str = "Student Loan Payment"
    session_file: str = "data/monarch_session.pickle"

    # Loan-group account setup / naming (used for auto-mapping + optional auto-creation).
    # Placeholders: {provider}, {provider_upper}, {provider_display}, {group}
    loan_account_name_template: str = "{provider}-{group}"

    # If enabled, `sync --auto-setup-accounts` (or future automation) can create missing manual accounts.
    # We keep this default False to avoid surprising writes to Monarch without explicit intent.
    auto_create_loan_accounts: bool = False

    @model_validator(mode="after")
    def _validate_auth(self) -> "MonarchConfig":
        if self.token:
            return self
        if self.email and self.password:
            return self
        raise ValueError("Monarch auth requires either monarch.token or monarch.email+monarch.password")


class StateConfig(BaseModel):
    db_path: str = "data/state.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file_path: str = "data/sync.log"


class LoanMapping(BaseModel):
    group: str
    monarch_account_id: str = ""
    monarch_account_name: str = ""


class AppConfig(BaseModel):
    servicer: ServicerConfig
    gmail_imap: GmailImapConfig
    monarch: MonarchConfig
    state: StateConfig = StateConfig()
    logging: LoggingConfig = LoggingConfig()
    loans: list[LoanMapping] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_keys(cls, data: object) -> object:
        # No legacy migrations currently; keep hook for future compatibility if needed.
        return data

def load_config(path: Union[str, Path]) -> AppConfig:
    p = Path(path)
    raw: dict = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw = _expand_env_vars(raw)  # supports ${ENV_VAR} in YAML

    merged = _deep_merge(_default_config_from_env(), raw)
    cfg = AppConfig.model_validate(merged)

    # Convenience: allow loan groups to be provided via environment (so most users only edit `.env`).
    if not cfg.loans:
        env_groups = _parse_loan_groups_env(os.getenv("LOAN_GROUPS", ""))
        if env_groups:
            cfg = cfg.model_copy(update={"loans": [LoanMapping(group=g) for g in env_groups]})

    return cfg


