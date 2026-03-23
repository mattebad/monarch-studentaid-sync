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


class _BodyOnlyPage:
    def __init__(self, body_text: str) -> None:
        self._body_text = body_text

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self._body_text


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


def test_non_posted_detection_finds_cancelled_dates() -> None:
    c = _client()
    body = """
    Payment History
    Payment Date Payment Amount Applied to Principal Applied to Interest Payment Type

    03/01/2025 $10.00 $0.00 $0.00 Canceled
    02/14/2025 $34,632.04 $0.00 $0.00
    Cancelled
    02/13/2025 $34,632.04 $15,606.93 $19,025.11 Electronic
    """.strip()

    got = c._non_posted_payment_dates_from_payment_activity_text(body)
    assert set(got.keys()) == {date(2025, 3, 1), date(2025, 2, 14)}
    assert all("cancel" in v for v in got.values())


def test_non_posted_detection_finds_pending_dates() -> None:
    c = _client()
    body = """
    Payment Date Payment Amount Applied to Principal Applied to Interest Payment Type

    01/15/2026 $500.00 $0.00 $0.00 Pending
    01/10/2026 $278.52 $184.12 $94.40 Electronic
    """.strip()

    got = c._non_posted_payment_dates_from_payment_activity_text(body)
    assert date(2026, 1, 15) in got
    assert got[date(2026, 1, 15)] == "pending"
    assert date(2026, 1, 10) not in got


def test_non_posted_detection_finds_scheduled_dates() -> None:
    c = _client()
    body = """
    02/01/2026 $250.00 $0.00 $0.00 Scheduled
    01/15/2026 $278.52 $184.12 $94.40 Electronic
    """.strip()

    got = c._non_posted_payment_dates_from_payment_activity_text(body)
    assert date(2026, 2, 1) in got
    assert got[date(2026, 2, 1)] == "scheduled"
    assert date(2026, 1, 15) not in got


def test_non_posted_detection_finds_processing_dates() -> None:
    c = _client()
    body = """
    02/15/2026 $300.00 $0.00 $0.00 Processing
    02/01/2026 $278.52 $184.12 $94.40 Electronic
    """.strip()

    got = c._non_posted_payment_dates_from_payment_activity_text(body)
    assert date(2026, 2, 15) in got
    assert got[date(2026, 2, 15)] == "processing"
    assert date(2026, 2, 1) not in got


def test_non_posted_detection_mixed_statuses() -> None:
    """Multiple non-posted statuses in one body text are all detected."""
    c = _client()
    body = """
    03/01/2026 $100.00 $0.00 $0.00 Pending
    02/15/2026 $200.00 $0.00 $0.00 Processing
    02/01/2026 $150.00 $0.00 $0.00 Cancelled
    01/15/2026 $278.52 $184.12 $94.40 Electronic
    """.strip()

    got = c._non_posted_payment_dates_from_payment_activity_text(body)
    assert len(got) == 3
    assert got[date(2026, 3, 1)] == "pending"
    assert got[date(2026, 2, 15)] == "processing"
    assert got[date(2026, 2, 1)] == "cancelled"
    assert date(2026, 1, 15) not in got


def test_payment_history_list_detection_matches_table_view() -> None:
    c = _client()
    body = """
    Payment Activity
    Payment History
    Payment Date
    Payment Amount
    Applied to Principal
    Applied to Interest
    01/26/2026 $278.52 $185.39 $93.13 Auto Debit
    """.strip()

    assert c._looks_like_payment_history_list(body) is True


def test_payment_detail_context_detection_rejects_list_view_text() -> None:
    c = _client()
    body = """
    Payment Activity
    Payment History
    Payment Date Payment Amount Applied to Principal Applied to Interest Payment Type
    01/26/2026 $278.52 $185.39 $93.13 Auto Debit
    12/26/2025 $278.52 $187.76 $90.76 Auto Debit
    """.strip()

    assert c._looks_like_payment_detail_context(body, expected_groups={"AA", "AB"}) is False


def test_payment_detail_context_detection_accepts_group_breakdown_text() -> None:
    c = _client()
    body = """
    Payment Date: 01/26/2026
    Loan Group: AA
    Total Applied
    $31.20
    Principal
    $19.93
    Interest
    $11.27
    """.strip()

    assert c._looks_like_payment_detail_context(body, expected_groups={"AA", "AB"}) is True


def test_looks_like_access_denied_matches_403_text() -> None:
    c = _client()
    page = _BodyOnlyPage("HTTP 403 Access denied.")
    assert c._looks_like_access_denied(page) is True


def test_looks_like_access_denied_matches_short_access_denied_text() -> None:
    c = _client()
    page = _BodyOnlyPage("Access denied")
    assert c._looks_like_access_denied(page) is True


def test_looks_like_access_denied_ignores_normal_page_content() -> None:
    c = _client()
    page = _BodyOnlyPage("Welcome back. Payment Activity is ready.")
    assert c._looks_like_access_denied(page) is False
