from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

from .cri.client import PortalCredentials, ServicerPortalClient
from .cri.mfa import poll_gmail_imap_for_code
from .config import load_config
from .logging_config import configure_logging
from .monarch.client import MonarchClient
from .servicers import KNOWN_SERVICERS, is_known_provider
from .state import StateStore
from .util.debug_bundle import create_debug_bundle
from .util.money import cents_to_money_str


logger = logging.getLogger("studentaid_monarch_sync")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studentaid_monarch_sync")
    p.add_argument(
        "--env-file",
        default=".env",
        help="Path to a dotenv file (default: .env). If missing, env vars must already be set.",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sync = sub.add_parser("sync", help="Sync StudentAid servicer loan balances + posted payments into Monarch")
    sync.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")
    sync.add_argument("--dry-run", action="store_true", help="Do not write to Monarch; log intended actions")
    sync.add_argument(
        "--dry-run-check-monarch",
        action="store_true",
        help="In dry-run mode, also login to Monarch and check for duplicates (date+amount+merchant) so you can preview what would be skipped.",
    )
    sync.add_argument(
        "--skip-monarch-preflight",
        action="store_true",
        help="Skip early Monarch auth validation (default: validate Monarch before portal login for real runs and --dry-run-check-monarch).",
    )
    sync.add_argument("--headful", action="store_true", help="Run browser headful (debug)")
    sync.add_argument(
        "--fresh-session",
        action="store_true",
        help="Do not reuse stored browser session (cookies/localStorage). Helpful for weird redirects.",
    )
    sync.add_argument(
        "--manual-mfa",
        action="store_true",
        help="In headful mode, pause and let you enter the MFA code manually in the browser (safer while debugging).",
    )
    sync.add_argument(
        "--print-mfa-code",
        action="store_true",
        help="Print the full MFA code to stdout (debug). Requires --headful; avoid using in unattended/logged environments.",
    )
    sync.add_argument("--slowmo-ms", type=int, default=0, help="Playwright slow motion in milliseconds (debug).")
    sync.add_argument("--step-debug", action="store_true", help="Save step-by-step screenshots under data/debug/.")
    sync.add_argument(
        "--step-delay-ms",
        type=int,
        default=0,
        help="Extra delay (ms) after each captured step screenshot (so you can watch the browser).",
    )
    sync.add_argument("--max-payments", type=int, default=10, help="Max payment detail entries to scan (default: 10)")
    sync.add_argument(
        "--payments-since",
        default="",
        help="Only create payment transactions for payments on/after this date (YYYY-MM-DD).",
    )

    list_accounts = sub.add_parser("list-monarch-accounts", help="List Monarch accounts (for mapping setup)")
    list_accounts.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")

    list_servicers = sub.add_parser(
        "list-servicers",
        help="List common StudentAid servicer provider slugs (non-exhaustive; custom providers are allowed)",
    )

    preflight = sub.add_parser(
        "preflight",
        help="Validate configuration and external dependencies (Monarch auth, Gmail IMAP). Does not run Playwright.",
    )
    preflight.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")
    preflight.add_argument("--skip-monarch", action="store_true", help="Skip Monarch auth check")
    preflight.add_argument("--skip-imap", action="store_true", help="Skip Gmail IMAP connectivity check")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path)

    # Default logging: can be overridden once config is loaded.
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"))

    if args.cmd == "preflight":
        cfg = load_config(args.config)
        configure_logging(level=cfg.logging.level, file_path=cfg.logging.file_path)
        logger.info("Starting preflight checks")

        if not args.skip_monarch:
            asyncio.run(_preflight_monarch(cfg))

        if not args.skip_imap:
            _preflight_gmail_imap(cfg)

        logger.info("Preflight OK")
        return 0

    if args.cmd == "sync":
        cfg = load_config(args.config)
        configure_logging(level=cfg.logging.level, file_path=cfg.logging.file_path)
        logger.info("Starting sync (dry_run=%s)", args.dry_run)

        provider = (cfg.servicer.provider or "servicer").strip().lower()
        try:
            if args.manual_mfa and not args.headful:
                raise SystemExit("--manual-mfa requires --headful (you must be able to interact with the browser).")
            if args.print_mfa_code and not args.headful:
                raise SystemExit("--print-mfa-code requires --headful (avoid leaking codes into unattended logs).")

            groups = [m.group for m in cfg.loans]
            if not groups:
                raise SystemExit("No loans configured. Add loan groups to config.yaml under 'loans:'")

            # Fail fast: validate Monarch auth before we spend time in Playwright.
            # - For real runs we *must* talk to Monarch, so validate now.
            # - For dry-run-check-monarch we also talk to Monarch, so validate now.
            needs_monarch = (not args.dry_run) or bool(args.dry_run_check_monarch)
            if needs_monarch and not args.skip_monarch_preflight:
                asyncio.run(_preflight_monarch(cfg))

            cutoff = None
            if args.payments_since:
                from datetime import date as _date

                cutoff = _date.fromisoformat(args.payments_since)

            state = StateStore(cfg.state.db_path)
            run_id = state.record_run_start()
            try:
                # Persist browser session per-provider so switching servicers doesn't reuse stale cookies.
                if not is_known_provider(provider):
                    logger.warning(
                        "Unknown servicer.provider=%r. If the standard pattern doesn't work, set servicer.base_url explicitly.",
                        provider,
                    )
                storage_state_path = f"data/servicer_storage_state_{provider}.json"
                # Backward compatibility: older versions used `data/cri_storage_state.json`.
                if (
                    provider == "cri"
                    and not Path(storage_state_path).exists()
                    and Path("data/cri_storage_state.json").exists()
                ):
                    storage_state_path = "data/cri_storage_state.json"

                portal = ServicerPortalClient(
                    base_url=cfg.servicer.base_url,
                    creds=PortalCredentials(username=cfg.servicer.username, password=cfg.servicer.password),
                )

                mfa_provider = lambda: poll_gmail_imap_for_code(cfg.gmail_imap, print_code=args.print_mfa_code)

                loan_snapshots, payment_allocations = portal.extract(
                    groups=groups,
                    headless=not args.headful,
                    storage_state_path=storage_state_path,
                    max_payments_to_scan=args.max_payments,
                    payments_since=cutoff,
                    mfa_code_provider=mfa_provider,
                    mfa_method=cfg.servicer.mfa_method,
                    force_fresh_session=args.fresh_session,
                    slow_mo_ms=args.slowmo_ms,
                    step_debug=args.step_debug,
                    step_delay_ms=args.step_delay_ms,
                    manual_mfa=args.manual_mfa,
                )

                # Safety: keep filtering here too, even though extraction now stops scanning older payments early.
                if cutoff:
                    payment_allocations = [p for p in payment_allocations if p.payment_date >= cutoff]

                logger.info(
                    "Portal extracted %d loan snapshots, %d payment allocations",
                    len(loan_snapshots),
                    len(payment_allocations),
                )

                if args.dry_run:
                    if args.dry_run_check_monarch:
                        asyncio.run(_log_dry_run_with_monarch(cfg, state, loan_snapshots, payment_allocations))
                    else:
                        _log_dry_run(cfg, state, loan_snapshots, payment_allocations)
                    state.record_run_finish(run_id, ok=True, message="dry-run")
                    return 0

                asyncio.run(_apply_monarch_updates(cfg, state, loan_snapshots, payment_allocations))
                state.record_run_finish(run_id, ok=True)
                return 0
            except Exception as e:
                state.record_run_finish(run_id, ok=False, message=str(e))
                raise
            finally:
                state.close()
        except Exception:
            # Auto-bundle debug artifacts + log for easy sharing.
            try:
                bundle = create_debug_bundle(
                    debug_dir="data/debug",
                    log_file=cfg.logging.file_path or "data/sync.log",
                    out_dir="data",
                    provider=provider,
                )
                logger.error("Wrote debug bundle: %s", bundle)
            except Exception:
                logger.debug("Failed to create debug bundle.", exc_info=True)
            raise

    if args.cmd == "list-monarch-accounts":
        cfg = load_config(args.config)
        configure_logging(level=cfg.logging.level, file_path=cfg.logging.file_path)
        asyncio.run(_list_monarch_accounts(cfg))
        return 0

    if args.cmd == "list-servicers":
        # Print only; no config/env required.
        for k in sorted(KNOWN_SERVICERS.keys()):
            info = KNOWN_SERVICERS[k]
            print(f"{info.provider}\t{info.display_name}")
        return 0

    raise AssertionError("Unhandled command")


async def _preflight_monarch(cfg) -> None:
    """
    Validate Monarch auth and basic config mapping without mutating anything.

    This is deliberately lightweight: it catches invalid/expired tokens and bad account/category mappings
    before we spend time logging into the StudentAid portal.
    """
    mc = MonarchClient(
        email=cfg.monarch.email,
        password=cfg.monarch.password,
        token=cfg.monarch.token,
        mfa_secret=cfg.monarch.mfa_secret,
        session_file=cfg.monarch.session_file,
    )

    try:
        await mc.login()
        accounts = await mc.list_accounts()  # forces an authenticated API call
        _ = await mc.get_category_id_by_name(cfg.monarch.transfer_category_name)

        # Ensure loan mappings resolve (fast: uses cached account list).
        for m in cfg.loans:
            await mc.resolve_account_id(account_id=m.monarch_account_id, account_name=m.monarch_account_name)

        logger.info("Monarch preflight OK (accounts=%d, transfer_category=%r)", len(accounts), cfg.monarch.transfer_category_name)
    except Exception as e:
        raise RuntimeError(
            "Monarch preflight failed. This often means your MONARCH_TOKEN/session is invalid/expired, "
            "or your loan account mappings are wrong. Fix Monarch auth (or delete data/monarch_session.pickle) "
            "before running sync."
        ) from e


def _preflight_gmail_imap(cfg) -> None:
    """
    Best-effort Gmail IMAP connectivity check (no code extraction).
    """
    import imaplib

    g = cfg.gmail_imap
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            mail.login(g.user, g.app_password)
            status, _ = mail.select(g.folder)
            if status != "OK":
                raise RuntimeError(f"IMAP select failed for folder={g.folder!r}: {status}")
        finally:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass

        logger.info("Gmail IMAP preflight OK (user=%r folder=%r)", g.user, g.folder)
    except Exception as e:
        raise RuntimeError(
            "Gmail IMAP preflight failed. Check GMAIL_IMAP_USER/GMAIL_IMAP_APP_PASSWORD and folder/label configuration."
        ) from e


def _log_dry_run(cfg, state: StateStore, loan_snapshots, payment_allocations) -> None:
    from datetime import date as _date

    today = _date.today()

    logger.info("Dry-run: balances")
    for snap in loan_snapshots:
        mapped = next((m for m in cfg.loans if m.group == snap.group), None)
        if not mapped:
            logger.info("  - %s: (no mapping) skip", snap.group)
            continue

        already = state.get_last_balance_date(snap.group)
        if already == today:
            logger.info("  - %s: already updated today; skip", snap.group)
        else:
            logger.info("  - %s: would set balance to %s", snap.group, cents_to_money_str(snap.outstanding_balance_cents))

    logger.info("Dry-run: payments")
    for alloc in payment_allocations:
        key = alloc.allocation_key()
        if state.has_processed_payment(key):
            continue
        logger.info(
            "  - %s %s: would create payment txn %s (principal %s / interest %s)",
            alloc.payment_date.isoformat(),
            alloc.group,
            cents_to_money_str(alloc.total_applied_cents),
            cents_to_money_str(alloc.principal_applied_cents),
            cents_to_money_str(alloc.interest_applied_cents),
        )


async def _log_dry_run_with_monarch(cfg, state: StateStore, loan_snapshots, payment_allocations) -> None:
    """
    Dry-run that queries Monarch so we can preview duplicate-guard behavior without writing anything.
    """
    from datetime import date as _date

    mc = MonarchClient(
        email=cfg.monarch.email,
        password=cfg.monarch.password,
        token=cfg.monarch.token,
        mfa_secret=cfg.monarch.mfa_secret,
        session_file=cfg.monarch.session_file,
    )
    await mc.login()

    transfer_category_id = await mc.get_category_id_by_name(cfg.monarch.transfer_category_name)
    _ = transfer_category_id  # appease linters; dry-run never writes

    # Map group -> monarch account id
    group_to_account_id: dict[str, str] = {}
    for m in cfg.loans:
        group_to_account_id[m.group] = await mc.resolve_account_id(
            account_id=m.monarch_account_id, account_name=m.monarch_account_name
        )

    # Reference: show the most recent transaction for each loan-group account so we can confirm
    # merchant naming + amount sign conventions.
    logger.info("Dry-run (Monarch check): most recent transaction per loan-group account")
    for group, acct_id in group_to_account_id.items():
        try:
            t = await mc.get_most_recent_transaction(account_id=acct_id)
            if not t:
                logger.info("  - %s: (no transactions found)", group)
                continue
            merchant = (t.get("merchant") or {}).get("name") or t.get("plaidName") or ""
            logger.info(
                "  - %s: %s amount=%s merchant=%s id=%s",
                group,
                t.get("date") or "?",
                t.get("amount"),
                merchant,
                t.get("id") or "",
            )
        except Exception:
            logger.debug("Failed to fetch most recent txn for group=%s account=%s", group, acct_id, exc_info=True)

    today = _date.today()

    logger.info("Dry-run (Monarch check): balances")
    for snap in loan_snapshots:
        acct_id = group_to_account_id.get(snap.group)
        if not acct_id:
            logger.info("  - %s: (no mapping) skip", snap.group)
            continue

        already = state.get_last_balance_date(snap.group)
        if already == today:
            logger.info("  - %s: already updated today; skip", snap.group)
        else:
            logger.info("  - %s: would set balance to %s", snap.group, cents_to_money_str(snap.outstanding_balance_cents))

    merchant_name = cfg.monarch.payment_merchant_name or "Student Loan Payment"
    logger.info("Dry-run (Monarch check): payments (dupe guard = date + amount + merchant=%r)", merchant_name)

    would_create = 0
    would_skip_state = 0
    would_skip_dup = 0

    for alloc in payment_allocations:
        acct_id = group_to_account_id.get(alloc.group)
        if not acct_id:
            logger.warning("No Monarch mapping for group=%s; skipping payment txn preview", alloc.group)
            continue

        key = alloc.allocation_key()
        if state.has_processed_payment(key):
            would_skip_state += 1
            continue

        # Match the real-run sign heuristic so the duplicate check is apples-to-apples.
        amount_cents = alloc.total_applied_cents
        existing_bal = await mc.get_account_display_balance(acct_id)
        if existing_bal > 0 and amount_cents < 0:
            amount_cents = abs(amount_cents)

        dup = await mc.find_duplicate_transaction(
            account_id=acct_id,
            posted_date_iso=alloc.payment_date.isoformat(),
            amount_cents=amount_cents,
            merchant_name=merchant_name,
        )
        if dup:
            would_skip_dup += 1
            logger.info(
                "  - %s %s: SKIP (duplicate) %s principal %s / interest %s (txn_id=%s)",
                alloc.payment_date.isoformat(),
                alloc.group,
                cents_to_money_str(amount_cents),
                cents_to_money_str(alloc.principal_applied_cents),
                cents_to_money_str(alloc.interest_applied_cents),
                dup.get("id") or "",
            )
            continue

        would_create += 1
        logger.info(
            "  - %s %s: CREATE %s principal %s / interest %s",
            alloc.payment_date.isoformat(),
            alloc.group,
            cents_to_money_str(amount_cents),
            cents_to_money_str(alloc.principal_applied_cents),
            cents_to_money_str(alloc.interest_applied_cents),
        )

    logger.info(
        "Dry-run (Monarch check) summary: create=%d skip_state=%d skip_duplicate=%d (total=%d)",
        would_create,
        would_skip_state,
        would_skip_dup,
        len(payment_allocations),
    )


async def _list_monarch_accounts(cfg) -> None:
    mc = MonarchClient(
        email=cfg.monarch.email,
        password=cfg.monarch.password,
        token=cfg.monarch.token,
        mfa_secret=cfg.monarch.mfa_secret,
        session_file=cfg.monarch.session_file,
    )
    await mc.login()
    accounts = await mc.list_accounts()

    for a in accounts:
        print(
            f"{a.get('id')} | {a.get('displayName')} | type={((a.get('type') or {}).get('name'))} "
            f"manual={a.get('isManual')} bal={a.get('displayBalance')}"
        )


async def _apply_monarch_updates(cfg, state: StateStore, loan_snapshots, payment_allocations) -> None:
    from datetime import date as _date

    mc = MonarchClient(
        email=cfg.monarch.email,
        password=cfg.monarch.password,
        token=cfg.monarch.token,
        mfa_secret=cfg.monarch.mfa_secret,
        session_file=cfg.monarch.session_file,
    )
    await mc.login()

    transfer_category_id = await mc.get_category_id_by_name(cfg.monarch.transfer_category_name)

    # Map group -> monarch account id
    group_to_account_id: dict[str, str] = {}
    for m in cfg.loans:
        group_to_account_id[m.group] = await mc.resolve_account_id(
            account_id=m.monarch_account_id, account_name=m.monarch_account_name
        )

    # Reference: show the most recent transaction for each loan-group account so we can confirm
    # merchant naming + amount sign conventions.
    logger.info("Monarch reference: most recent transaction per loan-group account")
    for group, acct_id in group_to_account_id.items():
        try:
            t = await mc.get_most_recent_transaction(account_id=acct_id)
            if not t:
                logger.info("  - %s: (no transactions found)", group)
                continue
            merchant = (t.get("merchant") or {}).get("name") or t.get("plaidName") or ""
            logger.info(
                "  - %s: %s amount=%s merchant=%s id=%s",
                group,
                t.get("date") or "?",
                t.get("amount"),
                merchant,
                t.get("id") or "",
            )
        except Exception:
            logger.debug("Failed to fetch most recent txn for group=%s account=%s", group, acct_id, exc_info=True)

    today = _date.today()

    # 1) Balance updates (once per day)
    for snap in loan_snapshots:
        acct_id = group_to_account_id.get(snap.group)
        if not acct_id:
            logger.warning("No Monarch mapping for group=%s; skipping balance update", snap.group)
            continue

        last = state.get_last_balance_date(snap.group)
        if last == today:
            continue

        # Heuristic: if Monarch displays the balance as negative, keep it negative.
        existing_bal = await mc.get_account_display_balance(acct_id)
        target_cents = snap.outstanding_balance_cents
        if existing_bal < 0 and target_cents > 0:
            target_cents = -target_cents

        await mc.update_account_balance(account_id=acct_id, balance_cents=target_cents)
        state.set_last_balance_date(snap.group, today)

    # 2) Payment transactions (idempotent)
    merchant_name = cfg.monarch.payment_merchant_name or "Student Loan Payment"
    for alloc in payment_allocations:
        acct_id = group_to_account_id.get(alloc.group)
        if not acct_id:
            logger.warning("No Monarch mapping for group=%s; skipping payment txn", alloc.group)
            continue

        key = alloc.allocation_key()
        if state.has_processed_payment(key):
            continue

        memo = (
            f"CRI payment allocation. TotalPayment={cents_to_money_str(alloc.payment_total_cents)} "
            f"Principal={cents_to_money_str(alloc.principal_applied_cents)} "
            f"Interest={cents_to_money_str(alloc.interest_applied_cents)}"
        )

        # Heuristic sign: if Monarch displays liabilities as negative, a payment should be a positive inflow.
        amount_cents = alloc.total_applied_cents
        existing_bal = await mc.get_account_display_balance(acct_id)
        if existing_bal > 0 and amount_cents < 0:
            amount_cents = abs(amount_cents)

        # Extra safety: if a matching transaction already exists in Monarch (same date + amount + merchant),
        # skip creation and mark it processed so we don't spam duplicates if the local SQLite state is reset.
        dup = await mc.find_duplicate_transaction(
            account_id=acct_id,
            posted_date_iso=alloc.payment_date.isoformat(),
            amount_cents=amount_cents,
            merchant_name=merchant_name,
        )
        if dup:
            logger.info(
                "Duplicate guard: found existing txn for %s %s amount=%s merchant=%s id=%s; skipping create",
                alloc.group,
                alloc.payment_date.isoformat(),
                cents_to_money_str(amount_cents),
                merchant_name,
                dup.get("id") or "",
            )
            state.mark_processed_payment(
                key=key,
                payment_date=alloc.payment_date,
                group_code=alloc.group,
                total_applied_cents=alloc.total_applied_cents,
                payment_total_cents=alloc.payment_total_cents,
                monarch_transaction_id=str(dup.get("id") or "") or None,
            )
            continue

        txn_id = await mc.create_payment_transaction(
            account_id=acct_id,
            posted_date_iso=alloc.payment_date.isoformat(),
            amount_cents=amount_cents,
            merchant_name=merchant_name,
            category_id=transfer_category_id,
            memo=memo,
            update_balance=False,
        )

        state.mark_processed_payment(
            key=key,
            payment_date=alloc.payment_date,
            group_code=alloc.group,
            total_applied_cents=alloc.total_applied_cents,
            payment_total_cents=alloc.payment_total_cents,
            monarch_transaction_id=txn_id or None,
        )


