from __future__ import annotations

from datetime import date

import pytest

from cri_monarch_sync.cri.client import PortalCredentials, ServicerPortalClient
from cri_monarch_sync.util.dates import parse_us_date


def _client() -> ServicerPortalClient:
    return ServicerPortalClient(
        base_url="https://example.studentaid.gov",
        creds=PortalCredentials(username="u", password="p"),
    )


def test_parse_us_date_basic() -> None:
    assert parse_us_date("12/26/2025") == date(2025, 12, 26)


def test_find_payment_date_prefers_labeled_date() -> None:
    c = _client()
    body = """
    Payment Date: 12/26/2025
    Statement Date: 12/30/2025
    """
    assert c._find_payment_date(body) == date(2025, 12, 26)


def test_find_payment_date_single_date_fallback() -> None:
    c = _client()
    body = "Some header\n12/26/2025\nSome footer\n"
    assert c._find_payment_date(body) == date(2025, 12, 26)


def test_find_payment_date_multiple_dates_raises() -> None:
    c = _client()
    body = "12/26/2025\n12/30/2025\n"
    with pytest.raises(RuntimeError):
        _ = c._find_payment_date(body)


def test_parse_payment_allocations_parses_groups_and_total_and_reference() -> None:
    c = _client()
    body = """
    Payment Date: 12/26/2025
    Confirmation Number: ABCD-1234

    AA  $31.20  $20.22  $10.98
    AB  $16.99  $10.61  $6.38
    Total $278.52 $184.12 $94.40
    """

    allocs = c._parse_payment_allocations(body)
    assert len(allocs) == 2
    assert {a.group for a in allocs} == {"AA", "AB"}
    assert all(a.payment_date == date(2025, 12, 26) for a in allocs)
    assert all(a.payment_reference == "ABCD-1234" for a in allocs)
    assert all(a.payment_total_cents == 27852 for a in allocs)

    aa = next(a for a in allocs if a.group == "AA")
    assert aa.total_applied_cents == 3120
    assert aa.principal_applied_cents == 2022
    assert aa.interest_applied_cents == 1098


def test_parse_payment_allocations_falls_back_to_sum_when_total_missing() -> None:
    c = _client()
    body = """
    Date: 12/26/2025
    AA  31.20  20.22  10.98
    AB  16.99  10.61  6.38
    """

    allocs = c._parse_payment_allocations(body)
    assert len(allocs) == 2
    assert all(a.payment_total_cents == (3120 + 1699) for a in allocs)


