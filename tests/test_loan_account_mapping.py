from __future__ import annotations

from pathlib import Path

from studentaid_monarch_sync.monarch.loan_accounts import (
    LoanAccountMapping,
    candidate_loan_account_names,
    find_exact_name_matches,
    load_loan_account_mapping,
    name_contains_group_token,
    render_loan_account_name,
    save_loan_account_mapping,
)


def test_render_loan_account_name_template() -> None:
    assert (
        render_loan_account_name(
            "Federal-{group}",
            group="aa",
            provider="nelnet",
            provider_display="Nelnet",
        )
        == "Federal-AA"
    )
    assert (
        render_loan_account_name(
            "{provider_upper}-{group}",
            group="AB",
            provider="cri",
            provider_display="Central Research, Inc. (CRI)",
        )
        == "CRI-AB"
    )


def test_candidate_loan_account_names_dedupes_and_includes_expected() -> None:
    names = candidate_loan_account_names(
        template="{provider}-{group}",
        group="AA",
        provider="nelnet",
        provider_display="Nelnet",
    )
    assert names[0] == "nelnet-AA"
    # includes generic fallbacks
    assert "Federal-AA" in names
    assert "Student Loan-AA" in names


def test_find_exact_name_matches_normalizes_whitespace_and_dashes() -> None:
    accounts = [
        {"id": "1", "displayName": "Federal AA", "isManual": True},
        {"id": "2", "displayName": "Something Else", "isManual": True},
    ]
    matches = find_exact_name_matches(accounts, ["Federal-AA"])
    assert len(matches) == 1
    assert matches[0]["id"] == "1"


def test_name_contains_group_token() -> None:
    assert name_contains_group_token("Federal-AA", group="AA") is True
    assert name_contains_group_token("Student Loan AA", group="AA") is True
    assert name_contains_group_token("AAA", group="AA") is False


def test_save_and_load_mapping_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "map.json"
    save_loan_account_mapping(
        p,
        provider="nelnet",
        name_template="{provider}-{group}",
        groups={
            "AA": LoanAccountMapping(account_id="acct-1", account_name="nelnet-AA"),
            "AB": LoanAccountMapping(account_id="acct-2", account_name="nelnet-AB"),
        },
    )
    loaded = load_loan_account_mapping(p)
    assert set(loaded.keys()) == {"AA", "AB"}
    assert loaded["AA"].account_id == "acct-1"
    assert loaded["AA"].account_name == "nelnet-AA"


