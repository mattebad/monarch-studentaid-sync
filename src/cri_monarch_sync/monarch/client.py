from __future__ import annotations

import logging
import os
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from monarchmoney import MonarchMoney

from ..util.money import cents_to_money_str


logger = logging.getLogger(__name__)


def _cents_to_dollars(cents: int) -> float:
    return float((Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _dollars_to_cents(amount: object) -> int:
    """
    Convert Monarch "amount" field (float/Decimal/str dollars) into integer cents.
    """
    if amount is None:
        return 0
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _txn_merchant_name(txn: Dict[str, Any]) -> str:
    merch = (txn.get("merchant") or {}).get("name") or ""
    if merch:
        return str(merch)
    return str(txn.get("plaidName") or "")


class MonarchClient:
    """
    Small wrapper around `monarchmoney.MonarchMoney` to:
    - login with saved session reuse
    - resolve account IDs by name
    - look up category IDs by name (Transfer)
    """

    def __init__(
        self,
        *,
        email: str,
        password: str,
        token: str,
        mfa_secret: str,
        session_file: str,
    ) -> None:
        self._email = email
        self._password = password
        self._token = token.strip() if token else ""
        self._mfa_secret = mfa_secret or None
        self._session_file = session_file
        self._mm = MonarchMoney(session_file=session_file, token=self._token or None)

        self._accounts_cache: Optional[List[Dict[str, Any]]] = None
        self._category_cache: Optional[List[Dict[str, Any]]] = None
        self._transactions_cache: dict[tuple[str, str, str, int, int, str], List[Dict[str, Any]]] = {}

    async def login(self) -> None:
        # Ensure the session directory exists (important for commands like list-monarch-accounts).
        session_path = Path(self._session_file)
        session_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) If we have a saved session token, use it (works with Sign in with Apple).
        if session_path.exists():
            try:
                self._mm.load_session(str(session_path))
                return
            except Exception:
                logger.warning("Failed to load saved Monarch session; will re-authenticate.", exc_info=True)
                try:
                    session_path.unlink()
                except Exception:
                    logger.debug("Failed to delete invalid Monarch session file.", exc_info=True)

        # 2) If caller provided a token, use it and persist it.
        if self._token:
            self._mm.save_session(str(session_path))
            return

        # 3) Fall back to email/password login.
        await self._mm.login(
            email=self._email,
            password=self._password,
            use_saved_session=True,
            save_session=True,
            mfa_secret_key=self._mfa_secret,
        )

    async def list_accounts(self) -> List[Dict[str, Any]]:
        if self._accounts_cache is None:
            resp = await self._mm.get_accounts()
            self._accounts_cache = resp.get("accounts", [])
        return list(self._accounts_cache)

    async def list_categories(self) -> List[Dict[str, Any]]:
        if self._category_cache is None:
            resp = await self._mm.get_transaction_categories()
            self._category_cache = resp.get("categories", [])
        return list(self._category_cache)

    async def resolve_account_id(self, *, account_id: str, account_name: str) -> str:
        if account_id:
            return account_id
        if not account_name:
            raise ValueError("Either monarch_account_id or monarch_account_name must be set")

        accounts = await self.list_accounts()
        matches = [a for a in accounts if (a.get("displayName") or "").strip().lower() == account_name.strip().lower()]
        if not matches:
            raise ValueError(f"Could not find Monarch account named '{account_name}'")
        if len(matches) > 1:
            raise ValueError(f"Multiple Monarch accounts matched name '{account_name}'; use account_id instead")
        return matches[0]["id"]

    async def get_category_id_by_name(self, name: str) -> str:
        cats = await self.list_categories()
        matches = [c for c in cats if (c.get("name") or "").strip().lower() == name.strip().lower()]
        if not matches:
            raise ValueError(f"Could not find Monarch category named '{name}'")
        if len(matches) > 1:
            # Prefer the system category if present
            sys = [c for c in matches if c.get("isSystemCategory") or c.get("systemCategory")]
            if len(sys) == 1:
                return sys[0]["id"]
            raise ValueError(f"Multiple categories matched '{name}'; cannot disambiguate")
        return matches[0]["id"]

    async def get_account_display_balance(self, account_id: str) -> float:
        accounts = await self.list_accounts()
        for a in accounts:
            if a.get("id") == account_id:
                return float(a.get("displayBalance") or 0)
        raise ValueError(f"Account id not found: {account_id}")

    async def update_account_balance(self, *, account_id: str, balance_cents: int) -> None:
        bal = _cents_to_dollars(balance_cents)
        logger.info("Updating Monarch account %s balance -> %s", account_id, cents_to_money_str(balance_cents))
        await self._mm.update_account(account_id=account_id, account_balance=bal)

    async def create_payment_transaction(
        self,
        *,
        account_id: str,
        posted_date_iso: str,
        amount_cents: int,
        merchant_name: str,
        category_id: str,
        memo: str,
        update_balance: bool = False,
    ) -> str:
        amt = _cents_to_dollars(amount_cents)
        logger.info(
            "Creating Monarch payment txn account=%s date=%s amount=%s",
            account_id,
            posted_date_iso,
            cents_to_money_str(amount_cents),
        )
        resp = await self._mm.create_transaction(
            date=posted_date_iso,
            account_id=account_id,
            amount=amt,
            merchant_name=merchant_name,
            category_id=category_id,
            notes=memo,
            update_balance=update_balance,
        )
        txn = (resp.get("createTransaction") or {}).get("transaction") or {}
        txn_id = txn.get("id") or ""
        return str(txn_id)

    async def list_transactions(
        self,
        *,
        account_id: str,
        start_date_iso: str = "",
        end_date_iso: str = "",
        limit: int = 100,
        offset: int = 0,
        search: str = "",
    ) -> List[Dict[str, Any]]:
        """
        List transactions for an account, optionally filtered by a date range.

        NOTE: Monarch's API requires BOTH start_date and end_date when filtering by date.
        """
        cache_key = (account_id, start_date_iso or "", end_date_iso or "", int(limit), int(offset), search or "")
        if cache_key in self._transactions_cache:
            return list(self._transactions_cache[cache_key])

        kwargs: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "search": search or "",
            "account_ids": [account_id],
        }
        if start_date_iso and end_date_iso:
            kwargs["start_date"] = start_date_iso
            kwargs["end_date"] = end_date_iso

        resp = await self._mm.get_transactions(**kwargs)
        txns = (resp.get("allTransactions") or {}).get("results") or []
        out = [dict(t) for t in txns]
        self._transactions_cache[cache_key] = out
        return list(out)

    async def get_most_recent_transaction(self, *, account_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the most recent transaction for an account (best-effort).
        """
        txns = await self.list_transactions(account_id=account_id, limit=1, offset=0)
        return txns[0] if txns else None

    async def find_duplicate_transaction(
        self,
        *,
        account_id: str,
        posted_date_iso: str,
        amount_cents: int,
        merchant_name: str,
        date_window_days: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """
        Look for an existing transaction matching date + amount + merchant.
        Returns the matching transaction dict if found.
        """
        start_dt = date.fromisoformat(posted_date_iso) - timedelta(days=date_window_days)
        end_dt = date.fromisoformat(posted_date_iso) + timedelta(days=date_window_days)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        txns = await self.list_transactions(
            account_id=account_id,
            start_date_iso=start_iso,
            end_date_iso=end_iso,
            limit=200,
            offset=0,
        )

        want_merchant = (merchant_name or "").strip().lower()
        for t in txns:
            if (t.get("date") or "") != posted_date_iso:
                continue
            if _dollars_to_cents(t.get("amount")) != int(amount_cents):
                continue
            got_merchant = _txn_merchant_name(t).strip().lower()
            if got_merchant != want_merchant:
                continue
            return t

        return None

