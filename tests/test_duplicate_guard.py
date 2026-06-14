from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from studentaid_monarch_sync.monarch.client import MonarchClient

DATE = "2026-01-15"
MERCHANT = "Student Loan Payment"
AMOUNT_CENTS = 10000  # $100.00


def _txn(txn_id: str, *, date: str = DATE, amount: float = 100.0, merchant: str = MERCHANT) -> Dict[str, Any]:
    return {"id": txn_id, "date": date, "amount": amount, "merchant": {"name": merchant}}


class _FakeMonarch(MonarchClient):
    """Bypass MonarchMoney/network; serve canned transactions to find_duplicate_transaction."""

    def __init__(self, txns: List[Dict[str, Any]]) -> None:
        self._txns = list(txns)

    async def list_transactions(self, **kwargs: Any) -> List[Dict[str, Any]]:  # type: ignore[override]
        return list(self._txns)


def _find(client: MonarchClient, **kwargs: Any) -> Dict[str, Any] | None:
    return asyncio.run(
        client.find_duplicate_transaction(
            account_id="acct",
            posted_date_iso=DATE,
            amount_cents=AMOUNT_CENTS,
            merchant_name=MERCHANT,
            **kwargs,
        )
    )


def test_strict_match_returns_on_date_amount_merchant() -> None:
    client = _FakeMonarch([_txn("1")])
    dup = _find(client)
    assert dup is not None and dup["id"] == "1"


def test_strict_default_does_not_match_when_merchant_differs() -> None:
    client = _FakeMonarch([_txn("1", merchant="EdFinancial")])
    assert _find(client) is None


def test_loose_mode_falls_back_to_date_amount_when_strict_misses() -> None:
    client = _FakeMonarch([_txn("1", merchant="EdFinancial")])
    dup = _find(client, loose_match=True)
    assert dup is not None and dup["id"] == "1"


def test_loose_mode_still_prefers_strict_hit() -> None:
    # Loose candidate appears first, strict hit second — strict must win.
    client = _FakeMonarch([_txn("loose", merchant="EdFinancial"), _txn("strict")])
    dup = _find(client, loose_match=True)
    assert dup is not None and dup["id"] == "strict"


def test_no_candidate_returns_none() -> None:
    # Different date → neither strict nor loose matches.
    client = _FakeMonarch([_txn("1", date="2026-02-01")])
    assert _find(client) is None
    assert _find(client, loose_match=True) is None


def test_amount_mismatch_is_not_a_loose_candidate() -> None:
    client = _FakeMonarch([_txn("1", amount=99.0, merchant="EdFinancial")])
    assert _find(client, loose_match=True) is None
