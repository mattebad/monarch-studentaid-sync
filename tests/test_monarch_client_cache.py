from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List

import pytest


def _import_monarch_client_with_fake_dep(monkeypatch):
    """
    Import `studentaid_monarch_sync.monarch.client` while providing a fake `monarchmoney` module.

    This keeps the unit tests fast/offline and validates our wrapper logic (cache invalidation and
    dedupe-on-create-error) without requiring the real Monarch client dependency.
    """

    class FakeCaptchaRequiredException(Exception):
        pass

    class FakeMonarchMoney:
        load_session_error: Exception | None = None
        login_error: Exception | None = None
        login_with_cookies_error: Exception | None = None
        get_accounts_error: Exception | None = None
        instances: List["FakeMonarchMoney"] = []
        CaptchaRequiredException = FakeCaptchaRequiredException

        def __init__(self, session_file: str, token: str | None = None) -> None:
            self.session_file = session_file
            self.token = token

            self._transactions: List[Dict[str, Any]] = []
            self.calls_get_transactions = 0
            self.fail_create_after_append = False
            self.fail_get_transactions_times = 0
            self.loaded_session_paths: List[str] = []
            self.saved_session_paths: List[str] = []
            self.login_calls: List[Dict[str, Any]] = []
            self.login_with_cookies_calls: List[Dict[str, Any]] = []
            self.calls_get_accounts = 0
            self.__class__.instances.append(self)

        # Session helpers used by MonarchClient.login()
        def load_session(self, _path: str) -> None:
            self.loaded_session_paths.append(_path)
            if self.__class__.load_session_error is not None:
                raise self.__class__.load_session_error
            return None

        def save_session(self, _path: str) -> None:
            self.saved_session_paths.append(_path)
            return None

        # Methods called by MonarchClient
        async def login(self, **_kwargs: Any) -> None:
            self.login_calls.append(dict(_kwargs))
            if self.__class__.login_error is not None:
                raise self.__class__.login_error
            if _kwargs.get("save_session", True):
                self.save_session(self.session_file)
            return None

        async def login_with_cookies(self, cookie_string: str, **_kwargs: Any) -> None:
            call = {"cookie_string": cookie_string, **_kwargs}
            self.login_with_cookies_calls.append(call)
            if self.__class__.login_with_cookies_error is not None:
                raise self.__class__.login_with_cookies_error
            if _kwargs.get("verify", True):
                await self.get_accounts()
            if _kwargs.get("save_session", True):
                self.save_session(self.session_file)
            return None

        async def get_accounts(self) -> Dict[str, Any]:
            self.calls_get_accounts += 1
            if self.__class__.get_accounts_error is not None:
                raise self.__class__.get_accounts_error
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
    fake_mod.CaptchaRequiredException = FakeCaptchaRequiredException
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
        cookie_string="",
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
        cookie_string="",
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
        cookie_string="",
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
        cookie_string="",
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
        cookie_string="",
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


def test_login_prefers_saved_session_over_cookie_bootstrap(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    session_file = tmp_path / "monarch_session.pickle"
    session_file.write_text("placeholder", encoding="utf-8")

    mc = MonarchClient(
        email="",
        password="",
        cookie_string="session_id=cookie; csrftoken=csrf",
        token="token",
        mfa_secret="",
        session_file=str(session_file),
    )

    asyncio.run(mc.login())

    assert mc._mm.loaded_session_paths == [str(session_file)]
    assert mc._mm.login_with_cookies_calls == []
    assert mc._mm.login_calls == []
    assert mc._mm.calls_get_accounts == 1


def test_login_bootstraps_from_cookie_string(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    session_file = tmp_path / "monarch_session.pickle"

    mc = MonarchClient(
        email="",
        password="",
        cookie_string="session_id=cookie; csrftoken=csrf",
        token="token",
        mfa_secret="",
        session_file=str(session_file),
    )

    asyncio.run(mc.login())

    assert mc._mm.loaded_session_paths == []
    assert mc._mm.login_with_cookies_calls == [
        {
            "cookie_string": "session_id=cookie; csrftoken=csrf",
            "save_session": True,
            "verify": True,
        }
    ]
    assert mc._mm.login_calls == []
    assert mc._mm.calls_get_accounts == 1
    assert mc._mm.saved_session_paths == [str(session_file)]


def test_login_falls_back_from_stale_session_to_cookie_string(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    FakeMonarchMoney.load_session_error = RuntimeError("stale session")
    session_file = tmp_path / "monarch_session.pickle"
    session_file.write_text("bad pickle", encoding="utf-8")

    mc = MonarchClient(
        email="",
        password="",
        cookie_string="session_id=cookie; csrftoken=csrf",
        token="token",
        mfa_secret="",
        session_file=str(session_file),
    )

    asyncio.run(mc.login())

    assert FakeMonarchMoney.instances[1].loaded_session_paths == [str(session_file)]
    assert mc._mm.login_with_cookies_calls == [
        {
            "cookie_string": "session_id=cookie; csrftoken=csrf",
            "save_session": True,
            "verify": True,
        }
    ]
    assert mc._mm.login_calls == []
    assert mc._mm.calls_get_accounts == 1
    assert not session_file.exists()


def test_login_uses_token_compatibility_path(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    session_file = tmp_path / "monarch_session.pickle"

    mc = MonarchClient(
        email="",
        password="",
        cookie_string="",
        token="legacy-token",
        mfa_secret="",
        session_file=str(session_file),
    )

    asyncio.run(mc.login())

    assert mc._mm.loaded_session_paths == []
    assert mc._mm.login_with_cookies_calls == []
    assert mc._mm.login_calls == []
    assert mc._mm.calls_get_accounts == 1
    assert mc._mm.saved_session_paths == [str(session_file)]


def test_login_password_captcha_guides_cookie_bootstrap(monkeypatch, tmp_path) -> None:
    MonarchClient, FakeMonarchMoney = _import_monarch_client_with_fake_dep(monkeypatch)
    FakeMonarchMoney.login_error = FakeMonarchMoney.CaptchaRequiredException("CAPTCHA_REQUIRED")
    session_file = tmp_path / "monarch_session.pickle"

    mc = MonarchClient(
        email="me@example.com",
        password="secret",
        cookie_string="",
        token="",
        mfa_secret="",
        session_file=str(session_file),
    )

    with pytest.raises(RuntimeError, match="MONARCH_COOKIE_STRING"):
        asyncio.run(mc.login())


