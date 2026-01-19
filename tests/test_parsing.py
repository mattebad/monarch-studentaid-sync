from __future__ import annotations

from datetime import date

import pytest

from studentaid_monarch_sync.portal.client import PortalCredentials, ServicerPortalClient
from studentaid_monarch_sync.util.dates import parse_us_date


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


def test_parse_payment_allocations_supports_hyphenated_group_ids() -> None:
    c = _client()
    body = """
    Payment Date: 12/26/2025

    1-01  $31.20  $20.22  $10.98
    1-02  $16.99  $10.61  $6.38
    Total $48.19 $30.83 $17.36
    """

    allocs = c._parse_payment_allocations(body)
    assert len(allocs) == 2
    assert {a.group for a in allocs} == {"1-01", "1-02"}
    assert all(a.payment_total_cents == 4819 for a in allocs)


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


def test_parse_payment_allocations_parses_multiline_table_cells() -> None:
    """
    Some servicer portals render allocation tables responsively, so each cell becomes its own line.
    """
    c = _client()
    body = """
    Payment Date: 12/26/2025
    Confirmation Number: ABCD-1234

    AA
    $31.20
    $20.22
    $10.98

    AB
    $16.99
    $10.61
    $6.38

    Total
    $278.52
    $184.12
    $94.40
    """

    allocs = c._parse_payment_allocations(body)
    assert len(allocs) == 2
    assert {a.group for a in allocs} == {"AA", "AB"}
    assert all(a.payment_date == date(2025, 12, 26) for a in allocs)
    assert all(a.payment_reference == "ABCD-1234" for a in allocs)
    assert all(a.payment_total_cents == 27852 for a in allocs)


def test_parse_payment_allocations_parses_row_with_prefix_text_when_expected_groups_given() -> None:
    """
    Some table rows include non-group text before the group (e.g. a details-toggle cell).
    When expected_groups is provided (runtime), we should still extract the correct group + amounts.
    """
    c = _client()
    body = """
    Payment Date: 12/26/2024

    Toggle details row AA  $25.71  $14.41  $11.30
    Toggle details row AB  $16.99  $9.52  $7.47
    """

    allocs = c._parse_payment_allocations(body, expected_groups={"AA", "AB"})
    assert len(allocs) == 2
    assert {a.group for a in allocs} == {"AA", "AB"}
    assert all(a.payment_date == date(2024, 12, 26) for a in allocs)
    assert all(a.payment_total_cents == (2571 + 1699) for a in allocs)
