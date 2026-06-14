from __future__ import annotations

from typing import List, Tuple

import pytest

from studentaid_monarch_sync.portal.client import PortalCredentials, ServicerPortalClient
from studentaid_monarch_sync.portal.selectors import PortalSelectors


def _client(*, account_number: str = "", ssn: str = "", dob: str = "01/02/1990") -> ServicerPortalClient:
    return ServicerPortalClient(
        base_url="https://edfinancial.studentaid.gov",
        creds=PortalCredentials(
            username="u",
            password="p",
            account_number=account_number,
            ssn=ssn,
            date_of_birth=dob,
        ),
    )


# ---------------------------------------------------------------------------
# _parse_dob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("03/07/1985", ("03", "07", "1985")),
        ("3-7-1985", ("03", "07", "1985")),
        ("1985-03-07", ("03", "07", "1985")),
        ("12/31/2000", ("12", "31", "2000")),
    ],
)
def test_parse_dob_formats(value: str, expected: Tuple[str, str, str]) -> None:
    assert ServicerPortalClient._parse_dob(value) == expected


def test_parse_dob_two_digit_year_boundary() -> None:
    # > 30 -> 19xx, <= 30 -> 20xx
    assert ServicerPortalClient._parse_dob("01/01/31")[2] == "1931"
    assert ServicerPortalClient._parse_dob("01/01/30")[2] == "2030"


@pytest.mark.parametrize("value", ["", "not a date", "01/2020", "1/2/3/4"])
def test_parse_dob_malformed(value: str) -> None:
    assert ServicerPortalClient._parse_dob(value) == ("", "", "")


# ---------------------------------------------------------------------------
# Fallback orchestration: _complete_device_challenge_with_fallback
# ---------------------------------------------------------------------------


def _script_attempts(client: ServicerPortalClient, results: dict[str, bool]) -> List[str]:
    """Patch the submit seam to return scripted clears per mode; record the attempt order."""
    attempts: List[str] = []

    def fake_submit(page, *, mode, acct, ssn, month, day, year, debug_dir):
        attempts.append(mode)
        return results.get(mode, False)

    client._edf_submit_device_challenge = fake_submit  # type: ignore[assignment]
    client._save_debug = lambda *a, **k: None  # type: ignore[assignment]
    return attempts


def _run_fallback(client: ServicerPortalClient, *, acct: str, ssn: str) -> None:
    client._complete_device_challenge_with_fallback(
        object(), acct=acct, ssn=ssn, month="01", day="02", year="1990", debug_dir="/tmp"
    )


def test_account_path_succeeds_without_ssn_attempt() -> None:
    client = _client(account_number="123", ssn="111223333")
    attempts = _script_attempts(client, {"account": True})
    _run_fallback(client, acct="123", ssn="111223333")
    assert attempts == ["account"]


def test_account_fails_then_ssn_succeeds() -> None:
    client = _client(account_number="123", ssn="111223333")
    attempts = _script_attempts(client, {"account": False, "ssn": True})
    _run_fallback(client, acct="123", ssn="111223333")
    assert attempts == ["account", "ssn"]


def test_account_fails_no_ssn_raises_actionable_error() -> None:
    client = _client(account_number="123", ssn="")
    attempts = _script_attempts(client, {"account": False})
    with pytest.raises(RuntimeError, match="SERVICER_SSN"):
        _run_fallback(client, acct="123", ssn="")
    assert attempts == ["account"]


def test_ssn_only_path_fails_raises() -> None:
    client = _client(account_number="", ssn="111223333")
    attempts = _script_attempts(client, {"ssn": False})
    with pytest.raises(RuntimeError, match="SSN"):
        _run_fallback(client, acct="", ssn="111223333")
    assert attempts == ["ssn"]


def test_both_paths_fail_raises_mentioning_both() -> None:
    client = _client(account_number="123", ssn="111223333")
    attempts = _script_attempts(client, {"account": False, "ssn": False})
    with pytest.raises(RuntimeError, match="both"):
        _run_fallback(client, acct="123", ssn="111223333")
    assert attempts == ["account", "ssn"]


# ---------------------------------------------------------------------------
# _edf_submit_device_challenge: "cleared" detection
# ---------------------------------------------------------------------------


class _Loc:
    def __init__(self, *, hidden: bool) -> None:
        self._hidden = hidden

    @property
    def first(self) -> "_Loc":
        return self

    def click(self) -> None:
        pass

    def wait_for(self, *, state: str, timeout: int) -> None:
        if state == "hidden" and not self._hidden:
            raise RuntimeError("still visible")

    def is_visible(self) -> bool:
        return not self._hidden


class _Page:
    def __init__(self, *, detect_hidden: bool) -> None:
        self._detect = _Loc(hidden=detect_hidden)
        self._other = _Loc(hidden=True)

    def locator(self, selector: str) -> _Loc:
        if selector == PortalSelectors().device_verify_detect:
            return self._detect
        return self._other


def _patch_fill_helpers(client: ServicerPortalClient) -> None:
    client._type_into = lambda *a, **k: None  # type: ignore[assignment]
    client._step = lambda *a, **k: None  # type: ignore[assignment]
    client._wait_for_settle = lambda *a, **k: None  # type: ignore[assignment]
    client._save_debug = lambda *a, **k: None  # type: ignore[assignment]


def test_submit_reports_cleared_when_detect_hidden() -> None:
    client = _client(account_number="123")
    _patch_fill_helpers(client)
    page = _Page(detect_hidden=True)
    cleared = client._edf_submit_device_challenge(
        page, mode="account", acct="123", ssn="", month="01", day="02", year="1990", debug_dir="/tmp"
    )
    assert cleared is True


def test_submit_reports_not_cleared_when_detect_still_visible() -> None:
    client = _client(account_number="123")
    _patch_fill_helpers(client)
    page = _Page(detect_hidden=False)
    cleared = client._edf_submit_device_challenge(
        page, mode="account", acct="123", ssn="", month="01", day="02", year="1990", debug_dir="/tmp"
    )
    assert cleared is False
