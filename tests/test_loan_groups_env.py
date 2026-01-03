from __future__ import annotations

from studentaid_monarch_sync.config import load_config


def test_loan_groups_can_come_from_env(tmp_path, monkeypatch) -> None:
    cfg_path = tmp_path / "missing.yaml"
    # Intentionally do not create the file; load_config should fall back to env-only defaults.

    monkeypatch.setenv("SERVICER_PROVIDER", "nelnet")
    monkeypatch.setenv("SERVICER_USERNAME", "u")
    monkeypatch.setenv("SERVICER_PASSWORD", "p")
    monkeypatch.setenv("GMAIL_IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("MONARCH_TOKEN", "dummy")
    monkeypatch.setenv("LOAN_GROUPS", "AA, AB")

    cfg = load_config(cfg_path)
    assert [m.group for m in cfg.loans] == ["AA", "AB"]


