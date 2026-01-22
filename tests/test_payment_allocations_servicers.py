from __future__ import annotations

from datetime import date

import pytest

from studentaid_monarch_sync.portal.client import PortalCredentials, ServicerPortalClient


def _client() -> ServicerPortalClient:
    # Construct a client only to reuse parsing helpers. No navigation is performed in these tests.
    return ServicerPortalClient(
        base_url="https://example.studentaid.gov",
        creds=PortalCredentials(username="u", password="p"),
    )


# These fixtures are intentionally plain-text because the runtime parser consumes Playwright's
# `page.inner_text("body")`, not raw HTML.

CRI_PAYMENT_DETAIL_TEXT = """
Payment Date: 12/26/2025
Confirmation Number: ABCD-1234

AA  $31.20  $20.22  $10.98
AB  $16.99  $10.61  $6.38
Total $48.19 $30.83 $17.36
""".strip()


NELNET_PAYMENT_DETAIL_TEXT = """
Payment History Details

Payment Date: 12/26/2024
Payment Amount: $240.05
Payment Type: Auto Debit
Account: E971914498

Details Group Total Applied Applied to Principal Applied to Interest
Toggle details row AA  $25.71  $14.41  $11.30
Toggle details row AB  $16.99  $9.52  $7.47
Toggle details row AC  $33.96  $19.02  $14.94
Toggle details row AD  $52.93  $31.28  $21.65
Toggle details row AE  $38.63  $24.60  $14.03
Toggle details row AF  $14.86  $9.46  $5.40
Toggle details row AG  $56.97  $32.90  $24.07
""".strip()


@pytest.mark.parametrize(
    "provider, body_text, expected_groups, expected_date, expected_total_cents, expected_rows",
    [
        (
            "cri",
            CRI_PAYMENT_DETAIL_TEXT,
            {"AA", "AB"},
            date(2025, 12, 26),
            4819,
            {
                "AA": (3120, 2022, 1098),
                "AB": (1699, 1061, 638),
            },
        ),
        (
            "nelnet",
            NELNET_PAYMENT_DETAIL_TEXT,
            {"AA", "AB", "AC", "AD", "AE", "AF", "AG"},
            date(2024, 12, 26),
            24005,
            {
                "AA": (2571, 1441, 1130),
                "AB": (1699, 952, 747),
                "AC": (3396, 1902, 1494),
                "AD": (5293, 3128, 2165),
                "AE": (3863, 2460, 1403),
                "AF": (1486, 946, 540),
                "AG": (5697, 3290, 2407),
            },
        ),
    ],
    ids=["cri", "nelnet"],
)
def test_parse_payment_allocations_across_servicers(
    provider: str,
    body_text: str,
    expected_groups: set[str],
    expected_date: date,
    expected_total_cents: int,
    expected_rows: dict[str, tuple[int, int, int]],
) -> None:
    """
    Regression coverage for payment allocation parsing across servicers.
    """
    c = _client()
    allocs = c._parse_payment_allocations(body_text, expected_groups=set(expected_groups))

    assert {a.group for a in allocs} == set(expected_rows.keys()), f"groups mismatch for provider={provider}"
    assert all(a.payment_date == expected_date for a in allocs), f"date mismatch for provider={provider}"
    assert all(a.payment_total_cents == expected_total_cents for a in allocs), f"total mismatch for provider={provider}"

    for a in allocs:
        want_total, want_principal, want_interest = expected_rows[a.group]
        assert a.total_applied_cents == want_total
        assert a.principal_applied_cents == want_principal
        assert a.interest_applied_cents == want_interest

