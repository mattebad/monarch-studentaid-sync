from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List


def _import_monarch_client_with_fake_dep(monkeypatch):
    """
    Import `studentaid_monarch_sync.monarch.client` while providing a fake `monarchmoney` module.

    This keeps the unit tests fast/offline and validates our wrapper logic (cache invalidation and
    dedupe-on-create-error) without requiring the real Monarch client dependency.
    """

    class FakeMonarchMoney:
        def __init__(self, session_file: str, token: str | None = None) -> None:
            self.session_file = session_file
            self.token = token

            self._transactions: List[Dict[str, Any]] = []
            self.calls_get_transactions = 0
            self.fail_create_after_append = False
            self.fail_get_transactions_times = 0

        # Session helpers used by MonarchClient.login()
        def load_session(self, _path: str) -> None:
            return None

        def save_session(self, _path: str) -> None:
            return None

        # Methods called by MonarchClient
        async def login(self, **_kwargs: Any) -> None:
            return None

        async def get_accounts(self) -> Dict[str, Any]:
            return {"accounts": [{"id": "acct1", "displayBalance": 0, "displayName": "Loan-AA", "isManual": True}]}

        async def get_transaction_categories(self) -> Dict[str, Any]:
            return {"categories": [{"id": "cat-transfer", "name": "Transfer"}]}

        async def get_account_type_options(self) -> Dict[str, Any]:
            return {"accountTypeOptions": []}

        async def update_account(self, **_kwargs: Any) -> Dict[str, Any]:
            return {}

        async def get_transactions(self, **kwargs: Any) -> Dict[str, Any]:
            self.calls_get_transactions += 1
            if self.fail_get_transactions_times > 0:
                self.fail_get_transactions_times -= 1
                raise RuntimeError("simulated transient get_transactions failure")
            account_id = (kwargs.get("account_ids") or [""])[0]
            start = kwargs.get("start_date") or ""
            end = kwargs.get("end_date") or ""
            limit = int(kwargs.get("limit") or 100)
            offset = int(kwargs.get("offset") or 0)
            search = (kwargs.get("search") or "").strip().lower()

            results = []
            for t in self._transactions:
                if t.get("accountId") != account_id:
                    continue
                if start and end:
                    if not (start <= (t.get("date") or "") <= end):
                        continue
                if search:
                    merch = ((t.get("merchant") or {}).get("name") or "").strip().lower()
                    notes = (t.get("notes") or "").strip().lower()
                    if search not in merch and search not in notes:
                        continue
                results.append(dict(t))
            return {"allTransactions": {"results": results[offset : offset + limit]}}

        async def create_transaction(self, **kwargs: Any) -> Dict[str, Any]:
            txn_id = f"txn-{len(self._transactions) + 1}"
            txn = {
                "id": txn_id,
                "date": kwargs.get("date"),
                "amount": kwargs.get("amount"),
                "merchant": {"name": kwargs.get("merchant_name")},
                "notes": kwargs.get("notes") or "",
                "accountId": kwargs.get("account_id"),
            }
            self._transactions.append(txn)

            if self.fail_create_after_append:
                raise RuntimeError("simulated timeout after server-side create")

            return {"createTransaction": {"transaction": {"id": txn_id}}}

    fake_mod = types.ModuleType("monarchmoney")
    fake_mod.MonarchMoney = FakeMonarchMoney
    monkeypatch.setitem(sys.modules, "monarchmoney", fake_mod)

    # Ensure the module imports fresh with our stub.
    sys.modules.pop("studentaid_monarch_sync.monarch.client", None)

    from studentaid_monarch_sync.monarch.client import MonarchClient

    return MonarchClient, FakeMonarchMoney


def test_create_payment_transaction_invalidates_transactions_cache(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    mc = MonarchClient(
        email="",
        password="",
        token="token",
        mfa_secret="",
        session_file=str(tmp_path / "monarch_session.pickle"),
    )
    assert isinstance(mc._mm, FakeMonarchMoney)

    # Prime the transactions cache for this day/account.
    txns0 = asyncio.run(
        mc.list_transactions(
            account_id="acct1",
            start_date_iso="2025-01-01",
            end_date_iso="2025-01-01",
            limit=200,
            offset=0,
        )
    )
    assert txns0 == []
    assert mc._mm.calls_get_transactions == 1

    # Create a new transaction; our wrapper should invalidate the cached day/account view.
    _ = asyncio.run(
        mc.create_payment_transaction(
            account_id="acct1",
            posted_date_iso="2025-01-01",
            amount_cents=123,
            merchant_name="US Department of Education",
            category_id="cat-transfer",
            memo="test",
            update_balance=False,
        )
    )

    txns1 = asyncio.run(
        mc.list_transactions(
            account_id="acct1",
            start_date_iso="2025-01-01",
            end_date_iso="2025-01-01",
            limit=200,
            offset=0,
        )
    )
    assert len(txns1) == 1
    assert mc._mm.calls_get_transactions == 2  # cache was invalidated and re-fetched


def test_create_payment_transaction_recovers_on_timeout_by_duplicate_guard(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    mc = MonarchClient(
        email="",
        password="",
        token="token",
        mfa_secret="",
        session_file=str(tmp_path / "monarch_session.pickle"),
    )
    assert isinstance(mc._mm, FakeMonarchMoney)

    # Simulate a timeout after the server has created the transaction but before the client got a response.
    mc._mm.fail_create_after_append = True

    txn_id = asyncio.run(
        mc.create_payment_transaction(
            account_id="acct1",
            posted_date_iso="2025-01-01",
            amount_cents=123,
            merchant_name="US Department of Education",
            category_id="cat-transfer",
            memo="test",
            update_balance=False,
        )
    )
    assert txn_id == "txn-1"


def test_find_duplicate_transaction_paginates(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    mc = MonarchClient(
        email="",
        password="",
        token="token",
        mfa_secret="",
        session_file=str(tmp_path / "monarch_session.pickle"),
    )
    assert isinstance(mc._mm, FakeMonarchMoney)

    # Fill the first page (200 txns) with non-matching items.
    for i in range(200):
        mc._mm._transactions.append(
            {
                "id": f"noise-{i}",
                "date": "2025-01-01",
                "amount": 0.01,
                "merchant": {"name": "Other Merchant"},
                "accountId": "acct1",
            }
        )

    # Place the target duplicate on the second page (offset 200).
    mc._mm._transactions.append(
        {
            "id": "target",
            "date": "2025-01-01",
            "amount": 1.23,  # 123 cents
            "merchant": {"name": "US Department of Education"},
            "accountId": "acct1",
        }
    )

    dup = asyncio.run(
        mc.find_duplicate_transaction(
            account_id="acct1",
            posted_date_iso="2025-01-01",
            amount_cents=123,
            merchant_name="US Department of Education",
        )
    )
    assert dup is not None
    assert dup.get("id") == "target"
    assert mc._mm.calls_get_transactions >= 2


def test_list_transactions_retries_transient_failure(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    mc = MonarchClient(
        email="",
        password="",
        token="token",
        mfa_secret="",
        session_file=str(tmp_path / "monarch_session.pickle"),
    )
    assert isinstance(mc._mm, FakeMonarchMoney)

    # Fail once; the wrapper should retry and succeed.
    mc._mm.fail_get_transactions_times = 1
    txns = asyncio.run(
        mc.list_transactions(
            account_id="acct1",
            start_date_iso="2025-01-01",
            end_date_iso="2025-01-01",
            limit=200,
            offset=0,
        )
    )
    assert txns == []
    assert mc._mm.calls_get_transactions == 2


def test_find_duplicate_transaction_respects_search(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    mc = MonarchClient(
        email="",
        password="",
        token="token",
        mfa_secret="",
        session_file=str(tmp_path / "monarch_session.pickle"),
    )
    assert isinstance(mc._mm, FakeMonarchMoney)

    # Two txns with identical date+amount+merchant but different notes.
    mc._mm._transactions.append(
        {
            "id": "t1",
            "date": "2025-01-01",
            "amount": 1.23,
            "merchant": {"name": "US Department of Education"},
            "notes": "Ref=REF1",
            "accountId": "acct1",
        }
    )
    mc._mm._transactions.append(
        {
            "id": "t2",
            "date": "2025-01-01",
            "amount": 1.23,
            "merchant": {"name": "US Department of Education"},
            "notes": "Ref=REF2",
            "accountId": "acct1",
        }
    )

    dup = asyncio.run(
        mc.find_duplicate_transaction(
            account_id="acct1",
            posted_date_iso="2025-01-01",
            amount_cents=123,
            merchant_name="US Department of Education",
            search="REF2",
        )
    )
    assert dup is not None
    assert dup.get("id") == "t2"


