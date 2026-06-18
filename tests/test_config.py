from __future__ import annotations

from pathlib import Path

import pytest

from studentaid_monarch_sync.cli import _build_monarch_cookie_string
from studentaid_monarch_sync.config import (
    ServicerConfig,
    _derive_provider_from_base_url,
    load_config,
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_derive_provider_from_base_url() -> None:
    assert _derive_provider_from_base_url("https://aidvantage.studentaid.gov") == "aidvantage"
    assert _derive_provider_from_base_url("https://nelnet.studentaid.gov/") == "nelnet"
    # even without scheme, we can still derive the slug (helper behavior)
    assert _derive_provider_from_base_url("mohela.studentaid.gov") == "mohela"


def test_servicer_base_url_defaulted_from_provider(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        "cfg.yaml",
        """
servicer:
  provider: "nelnet"
  base_url: ""
  username: "u"
  password: "p"
gmail_imap:
  user: "me@gmail.com"
  app_password: "app-pass"
monarch:
  token: "dummy"
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.servicer.provider == "nelnet"
    assert cfg.servicer.base_url == "https://nelnet.studentaid.gov"


def test_servicer_provider_derived_from_base_url(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        "cfg.yaml",
        """
servicer:
  provider: ""
  base_url: "https://mohela.studentaid.gov/"
  username: "u"
  password: "p"
gmail_imap:
  user: "me@gmail.com"
  app_password: "app-pass"
monarch:
  token: "dummy"
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.servicer.provider == "mohela"
    assert cfg.servicer.base_url == "https://mohela.studentaid.gov"


def test_servicer_requires_provider_or_base_url(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        "cfg.yaml",
        """
servicer:
  provider: ""
  base_url: ""
  username: "u"
  password: "p"
gmail_imap:
  user: "me@gmail.com"
  app_password: "app-pass"
monarch:
  token: "dummy"
""",
    )
    with pytest.raises(Exception):
        _ = load_config(cfg_path)


def _servicer(ssn: str) -> ServicerConfig:
    return ServicerConfig(provider="edfinancial", username="u", password="p", ssn=ssn)


@pytest.mark.parametrize("ssn", ["123456789", "123-45-6789", "123 45 6789", ""])
def test_servicer_ssn_accepts_valid_and_empty(ssn: str) -> None:
    # 9 digits (with or without separators) pass; empty is allowed since SSN is optional.
    cfg = _servicer(ssn)
    assert cfg.ssn == ssn


@pytest.mark.parametrize("ssn", ["12345678", "1234567890", "123-45-678", "12-345-67890"])
def test_servicer_ssn_rejects_wrong_length(ssn: str) -> None:
    with pytest.raises(ValueError, match="ssn"):
        _servicer(ssn)


def test_invalid_provider_slug_rejected(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        "cfg.yaml",
        """
servicer:
  provider: "nelnet!"
  base_url: ""
  username: "u"
  password: "p"
gmail_imap:
  user: "me@gmail.com"
  app_password: "app-pass"
monarch:
  token: "dummy"
""",
    )
    with pytest.raises(Exception):
        _ = load_config(cfg_path)


def test_monarch_cookie_string_comes_from_env(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "missing.yaml"

    monkeypatch.setenv("SERVICER_PROVIDER", "nelnet")
    monkeypatch.setenv("SERVICER_USERNAME", "u")
    monkeypatch.setenv("SERVICER_PASSWORD", "p")
    monkeypatch.setenv("GMAIL_IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("MONARCH_COOKIE_STRING", "session_id=abc; csrftoken=def")

    cfg = load_config(cfg_path)
    assert cfg.monarch.cookie_string == "session_id=abc; csrftoken=def"


def test_monarch_cookie_string_comes_from_split_env_vars(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "missing.yaml"

    monkeypatch.setenv("SERVICER_PROVIDER", "nelnet")
    monkeypatch.setenv("SERVICER_USERNAME", "u")
    monkeypatch.setenv("SERVICER_PASSWORD", "p")
    monkeypatch.setenv("GMAIL_IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("MONARCH_COOKIE_SESSION_ID", "abc")
    monkeypatch.setenv("MONARCH_COOKIE_CSRFTOKEN", "def")

    cfg = load_config(cfg_path)
    assert cfg.monarch.cookie_string == "session_id=abc; csrftoken=def"


def test_build_monarch_cookie_string() -> None:
    assert _build_monarch_cookie_string(session_id="abc", csrftoken="def") == "session_id=abc; csrftoken=def"


