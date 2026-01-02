from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from typing import Union
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


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
    # e.g. "cri.studentaid.gov" -> "cri"
    if "." in host:
        return host.split(".", 1)[0]
    return host


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
            raise ValueError("servicer.provider is required (e.g. 'cri', 'nelnet', 'mohela')")
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
        """
        Backward compatibility:
        - Older configs used a top-level `cri:` block.
        - New configs use `servicer:` + `servicer.provider`.
        """
        if not isinstance(data, dict):
            return data

        if "servicer" not in data and "cri" in data and isinstance(data["cri"], dict):
            legacy = dict(data["cri"])
            legacy.setdefault("provider", "cri")
            out = dict(data)
            out["servicer"] = legacy
            return out

        return data

def load_config(path: Union[str, Path]) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    expanded = _expand_env_vars(raw)
    return AppConfig.model_validate(expanded)


