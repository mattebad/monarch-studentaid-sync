from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

from .portal.client import PortalCredentials, ServicerPortalClient
from .portal.mfa import poll_gmail_imap_for_code
from .config import load_config
from .logging_config import configure_logging
from .monarch.client import MonarchClient
from .monarch.loan_accounts import (
    DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE,
    LoanAccountMapping,
    candidate_loan_account_names,
    default_mapping_path,
    find_exact_name_matches,
    load_loan_account_mapping,
    name_contains_group_token,
    normalize_group,
    render_loan_account_name,
    save_loan_account_mapping,
)
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
        "--allow-empty-loans",
        action="store_true",
        help=(
            "If the servicer shows an empty/zero-balance loan summary with no Group sections, continue without loan "
            "snapshots (useful for testing or closed accounts). Default: false (missing groups is an error)."
        ),
    )
    sync.add_argument(
        "--payments-since",
        default="",
        help="Only create payment transactions for payments on/after this date (YYYY-MM-DD).",
    )
    sync.add_argument(
        "--auto-setup-accounts",
        action="store_true",
        help="If loan groups are not mapped to Monarch account IDs yet, attempt to auto-map (and optionally create) manual accounts and persist the mapping under data/.",
    )

    list_accounts = sub.add_parser("list-monarch-accounts", help="List Monarch accounts (for mapping setup)")
    list_accounts.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")

    setup_accounts = sub.add_parser(
        "setup-monarch-accounts",
        help="Map (and optionally create) Monarch manual accounts for your loan groups, so you never have to copy/paste account IDs.",
    )
    setup_accounts.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")
    setup_accounts.add_argument(
        "--name-template",
        default="",
        help=(
            "Account naming template when creating new manual accounts. "
            "Placeholders: {provider}, {provider_upper}, {provider_display}, {group}. "
            "Default: monarch.loan_account_name_template (or '{provider}-{group}')."
        ),
    )
    setup_accounts.add_argument(
        "--apply",
        action="store_true",
        help="Actually create missing accounts and write the mapping file. Without this flag, prints what it would do.",
    )
    setup_accounts.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive: accept best guesses and create missing accounts without prompting.",
    )
    setup_accounts.add_argument(
        "--no-create",
        action="store_true",
        help="Do not create any new manual accounts (only map existing).",
    )
    setup_accounts.add_argument(
        "--out",
        default="",
        help="Optional output path for the mapping JSON (default: data/monarch_loan_accounts_{provider}.json)",
    )

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

    list_groups = sub.add_parser(
        "list-loan-groups",
        help="Log into your servicer portal and list discovered loan groups (helps you set LOAN_GROUPS).",
    )
    list_groups.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")
    list_groups.add_argument("--headful", action="store_true", help="Run browser headful (debug)")
    list_groups.add_argument(
        "--fresh-session",
        action="store_true",
        help="Do not reuse stored browser session (cookies/localStorage). Helpful for weird redirects.",
    )
    list_groups.add_argument(
        "--manual-mfa",
        action="store_true",
        help="In headful mode, pause and let you enter the MFA code manually in the browser (safer while debugging).",
    )
    list_groups.add_argument(
        "--print-mfa-code",
        action="store_true",
        help="Print the full MFA code to stdout (debug). Requires --headful; avoid using in unattended/logged environments.",
    )
    list_groups.add_argument("--slowmo-ms", type=int, default=0, help="Playwright slow motion in milliseconds (debug).")
    list_groups.add_argument("--step-debug", action="store_true", help="Save step-by-step screenshots under data/debug/.")
    list_groups.add_argument(
        "--step-delay-ms",
        type=int,
        default=0,
        help="Extra delay (ms) after each captured step screenshot (so you can watch the browser).",
    )

    browse = sub.add_parser(
        "browse-portal",
        help=(
            "Open a Playwright browser for manual portal exploration while automatically capturing "
            "HTML + screenshots on navigation. A zip bundle is written when you close the browser."
        ),
    )
    browse.add_argument("--config", default="config.yaml", help="Path to YAML config (default: config.yaml)")
    browse.add_argument(
        "--no-login",
        action="store_true",
        help="Do not auto-login; just open the portal and let you log in manually.",
    )
    browse.add_argument(
        "--manual-mfa",
        action="store_true",
        help="If auto-login hits MFA, pause and let you complete MFA manually in the browser (requires headful).",
    )
    browse.add_argument(
        "--print-mfa-code",
        action="store_true",
        help="Print the full MFA code to stdout (debug). Avoid using in unattended/logged environments.",
    )
    browse.add_argument("--fresh-session", action="store_true", help="Do not reuse stored browser session.")
    browse.add_argument("--slowmo-ms", type=int, default=0, help="Playwright slow motion in milliseconds (debug).")
    browse.add_argument(
        "--out-dir",
        default="data/debug",
        help="Directory to write the resulting debug bundle zip (default: data/debug).",
    )
    browse.add_argument(
        "--capture-dir",
        default="",
        help="Optional directory to write captures (default: auto under data/).",
    )
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
            _require_monarch_auth(cfg)
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
            t0 = time.time()
            if args.manual_mfa and not args.headful:
                raise SystemExit("--manual-mfa requires --headful (you must be able to interact with the browser).")
            if args.print_mfa_code and not args.headful:
                raise SystemExit("--print-mfa-code requires --headful (avoid leaking codes into unattended logs).")

            groups = [m.group for m in cfg.loans]
            if not groups:
                raise SystemExit("No loans configured. Set LOAN_GROUPS in .env (recommended), or add loan groups under 'loans:' in a YAML config.")

            if args.auto_setup_accounts and args.dry_run:
                raise SystemExit(
                    "--auto-setup-accounts may create Monarch manual accounts and is not allowed with --dry-run. "
                    "Run `studentaid_monarch_sync setup-monarch-accounts --apply` first, then run sync."
                )

            # Fail fast: validate Monarch auth before we spend time in Playwright.
            # - For real runs we *must* talk to Monarch, so validate now.
            # - For dry-run-check-monarch we also talk to Monarch, so validate now.
            needs_monarch = (not args.dry_run) or bool(args.dry_run_check_monarch)
            if needs_monarch:
                _require_monarch_auth(cfg)
            if needs_monarch and not args.skip_monarch_preflight:
                asyncio.run(_preflight_monarch(cfg, check_mappings=not bool(args.auto_setup_accounts)))

            cutoff = None
            if args.payments_since:
                from datetime import date as _date

                cutoff = _date.fromisoformat(args.payments_since)

            state = StateStore(cfg.state.db_path)
            run_id = state.record_run_start()
            logger.info("Run started (run_id=%s provider=%s groups=%s)", run_id, provider, ",".join(groups))
            try:
                # Persist browser session per-provider so switching servicers doesn't reuse stale cookies.
                if not is_known_provider(provider):
                    logger.warning(
                        "Unknown servicer.provider=%r. If the standard pattern doesn't work, set servicer.base_url explicitly.",
                        provider,
                    )
                storage_state_path = f"data/servicer_storage_state_{provider}.json"

                portal = ServicerPortalClient(
                    base_url=cfg.servicer.base_url,
                    creds=PortalCredentials(username=cfg.servicer.username, password=cfg.servicer.password),
                )

                mfa_provider = lambda: poll_gmail_imap_for_code(cfg.gmail_imap, print_code=args.print_mfa_code)

                t_portal = time.time()
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
                    allow_empty_loans=args.allow_empty_loans,
                )
                logger.info("Portal extract complete (seconds=%.2f)", time.time() - t_portal)

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
                        t_monarch = time.time()
                        asyncio.run(_log_dry_run_with_monarch(cfg, state, loan_snapshots, payment_allocations))
                        logger.info("Dry-run Monarch check complete (seconds=%.2f)", time.time() - t_monarch)
                    else:
                        _log_dry_run(cfg, state, loan_snapshots, payment_allocations)
                    state.record_run_finish(run_id, ok=True, message="dry-run")
                    logger.info("Run finished (run_id=%s ok=true seconds=%.2f)", run_id, time.time() - t0)
                    return 0

                t_monarch = time.time()
                asyncio.run(
                    _apply_monarch_updates(
                        cfg,
                        state,
                        loan_snapshots,
                        payment_allocations,
                        auto_setup_accounts=bool(args.auto_setup_accounts),
                    )
                )
                logger.info("Monarch updates complete (seconds=%.2f)", time.time() - t_monarch)
                state.record_run_finish(run_id, ok=True)
                logger.info("Run finished (run_id=%s ok=true seconds=%.2f)", run_id, time.time() - t0)
                return 0
            except Exception as e:
                state.record_run_finish(run_id, ok=False, message=str(e))
                logger.error("Run failed (run_id=%s ok=false seconds=%.2f)", run_id, time.time() - t0)
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
        _require_monarch_auth(cfg)
        asyncio.run(_list_monarch_accounts(cfg))
        return 0

    if args.cmd == "setup-monarch-accounts":
        cfg = load_config(args.config)
        configure_logging(level=cfg.logging.level, file_path=cfg.logging.file_path)
        _require_monarch_auth(cfg)
        asyncio.run(
            _setup_monarch_accounts(
                cfg,
                apply=args.apply,
                yes=args.yes,
                no_create=args.no_create,
                name_template=args.name_template,
                out_path=args.out,
            )
        )
        return 0

    if args.cmd == "list-loan-groups":
        cfg = load_config(args.config)
        configure_logging(level=cfg.logging.level, file_path=cfg.logging.file_path)

        if args.manual_mfa and not args.headful:
            raise SystemExit("--manual-mfa requires --headful (you must be able to interact with the browser).")
        if args.print_mfa_code and not args.headful:
            raise SystemExit("--print-mfa-code requires --headful (avoid leaking codes into unattended logs).")

        provider = (cfg.servicer.provider or "servicer").strip().lower()
        storage_state_path = f"data/servicer_storage_state_{provider}.json"

        portal = ServicerPortalClient(
            base_url=cfg.servicer.base_url,
            creds=PortalCredentials(username=cfg.servicer.username, password=cfg.servicer.password),
        )
        mfa_provider = lambda: poll_gmail_imap_for_code(cfg.gmail_imap, print_code=args.print_mfa_code)

        groups = portal.discover_loan_groups(
            headless=not args.headful,
            storage_state_path=storage_state_path,
            mfa_code_provider=mfa_provider,
            mfa_method=cfg.servicer.mfa_method,
            force_fresh_session=args.fresh_session,
            slow_mo_ms=args.slowmo_ms,
            step_debug=args.step_debug,
            step_delay_ms=args.step_delay_ms,
            manual_mfa=args.manual_mfa,
        )

        if not groups:
            print("No loan groups found.")
            return 0

        print("Discovered loan groups:")
        suggested: list[str] = []
        seen: set[str] = set()
        for token, label in groups:
            tok = (token or "").strip()
            lab = (label or "").strip()
            if tok:
                suggested.append(tok)
            if tok and tok not in seen:
                seen.add(tok)
            print(f"- {tok or '(no token)'}\t{lab}")

        if suggested:
            # De-dupe suggested tokens, preserve order.
            out: list[str] = []
            seen2: set[str] = set()
            for t in suggested:
                if t in seen2:
                    continue
                out.append(t)
                seen2.add(t)
            print()
            print("Suggested .env value:")
            print(f"LOAN_GROUPS={','.join(out)}")

        return 0

    if args.cmd == "list-servicers":
        # Print only; no config/env required.
        for k in sorted(KNOWN_SERVICERS.keys()):
            info = KNOWN_SERVICERS[k]
            print(f"{info.provider}\t{info.display_name}")
        return 0

    if args.cmd == "browse-portal":
        cfg = load_config(args.config)
        configure_logging(level=cfg.logging.level, file_path=cfg.logging.file_path)

        provider = (cfg.servicer.provider or "servicer").strip().lower()
        storage_state_path = f"data/servicer_storage_state_{provider}.json"

        portal = ServicerPortalClient(
            base_url=cfg.servicer.base_url,
            creds=PortalCredentials(username=cfg.servicer.username, password=cfg.servicer.password),
        )

        mfa_provider = lambda: poll_gmail_imap_for_code(cfg.gmail_imap, print_code=args.print_mfa_code)

        try:
            out_zip = portal.browse_and_capture(
                debug_dir=args.capture_dir or "",
                log_file=cfg.logging.file_path,
                out_dir=args.out_dir,
                headless=False,
                storage_state_path=storage_state_path,
                mfa_code_provider=mfa_provider,
                mfa_method=cfg.servicer.mfa_method,
                force_fresh_session=args.fresh_session,
                slow_mo_ms=args.slowmo_ms,
                manual_mfa=args.manual_mfa,
                no_login=args.no_login,
            )
            print(f"✅ Debug bundle written: {out_zip}")
            return 0
        except KeyboardInterrupt:
            print("Interrupted. (If any captures were created, check the data/ directory for a debug_bundle zip.)")
            return 130
        except Exception as e:
            print(f"❌ browse-portal failed: {e}")
            return 1

    raise AssertionError("Unhandled command")


def _require_monarch_auth(cfg) -> None:
    m = cfg.monarch
    if getattr(m, "token", ""):
        return
    if getattr(m, "email", "") and getattr(m, "password", ""):
        return
    raise SystemExit("Missing Monarch auth. Set MONARCH_TOKEN or MONARCH_EMAIL + MONARCH_PASSWORD in your .env.")


async def _preflight_monarch(cfg, *, check_mappings: bool = True) -> None:
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

        # Ensure loan groups map to Monarch accounts (no writes in preflight).
        if check_mappings:
            _ = await _resolve_monarch_loan_group_accounts(
                cfg,
                mc,
                allow_create=False,
                yes=False,
                interactive=False,
            )

        logger.info("Monarch preflight OK (accounts=%d, transfer_category=%r)", len(accounts), cfg.monarch.transfer_category_name)
    except Exception as e:
        raise RuntimeError(
            "Monarch preflight failed. This often means your MONARCH_TOKEN/session is invalid/expired, "
            "or your loan account mappings are missing/wrong. Fix Monarch auth (or delete data/monarch_session.pickle), "
            "then run `studentaid_monarch_sync setup-monarch-accounts --apply` to auto-create/map accounts."
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

    # Map group -> monarch account id (read-only; never creates accounts in dry-run-check-monarch).
    group_to_account_id = await _resolve_monarch_loan_group_accounts(
        cfg,
        mc,
        allow_create=False,
        yes=False,
        interactive=False,
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
            search=(alloc.payment_reference or "").strip()
            if getattr(cfg.monarch, "duplicate_guard_use_reference", False)
            else "",
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


def _servicer_display_name(provider: str) -> str:
    info = KNOWN_SERVICERS.get((provider or "").strip().lower())
    return info.display_name if info else (provider or "").strip()


def _prompt_choice(prompt: str, *, min_value: int, max_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw:
            continue
        if raw.lower() in {"q", "quit", "exit"}:
            raise SystemExit("Aborted.")
        if raw.isdigit():
            n = int(raw)
            if min_value <= n <= max_value:
                return n
        print(f"Enter a number from {min_value} to {max_value} (or 'q' to abort).")


async def _resolve_monarch_loan_group_accounts(
    cfg,
    mc: MonarchClient,
    *,
    allow_create: bool,
    yes: bool,
    interactive: bool,
    name_template_override: str = "",
    mapping_path_override: str = "",
) -> dict[str, str]:
    """
    Resolve loan group -> Monarch account id using:
    1) config monarch_account_id (stable)
    2) mapping file under data/ (stable, rename-safe)
    3) config monarch_account_name (fallback)
    4) derived name template + heuristics (first-time setup convenience)

    If allow_create=True, missing groups can be created as new Monarch manual accounts.
    """
    provider = (cfg.servicer.provider or "servicer").strip().lower()
    provider_display = _servicer_display_name(provider) or provider

    template = (name_template_override or cfg.monarch.loan_account_name_template or DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE).strip()
    if not template:
        template = DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE

    mapping_path = Path(mapping_path_override) if mapping_path_override else default_mapping_path(provider)
    mapping = load_loan_account_mapping(mapping_path)

    accounts = await mc.list_accounts()
    account_by_id: dict[str, dict] = {str(a.get("id")): a for a in accounts if a.get("id")}
    manual_accounts = [a for a in accounts if a.get("isManual")]

    changed = False
    group_to_account_id: dict[str, str] = {}
    missing: list[str] = []

    async def _refresh_accounts() -> None:
        nonlocal accounts, account_by_id, manual_accounts
        accounts = await mc.list_accounts()
        account_by_id = {str(a.get("id")): a for a in accounts if a.get("id")}
        manual_accounts = [a for a in accounts if a.get("isManual")]

    for loan in cfg.loans:
        group = normalize_group(loan.group)
        if not group:
            continue

        acct_id = ""

        # 1) explicit config ID
        if loan.monarch_account_id:
            acct_id = str(loan.monarch_account_id).strip()

        # 2) mapping file
        if not acct_id and group in mapping:
            acct_id = mapping[group].account_id

        # 3) explicit config name
        if not acct_id and loan.monarch_account_name:
            acct_id = await mc.resolve_account_id(account_id="", account_name=loan.monarch_account_name)

        # 4) name template + heuristics
        if not acct_id:
            wanted_names = candidate_loan_account_names(
                template=template,
                group=group,
                provider=provider,
                provider_display=provider_display,
            )

            exact = find_exact_name_matches(manual_accounts, wanted_names)
            if len(exact) == 1:
                acct_id = str(exact[0].get("id") or "").strip()
            elif len(exact) > 1:
                if yes:
                    acct_id = str(exact[0].get("id") or "").strip()
                    logger.warning(
                        "Multiple Monarch manual accounts matched group=%s by name; choosing the first match (%s).",
                        group,
                        str(exact[0].get("displayName") or ""),
                    )
                elif interactive and sys.stdin.isatty():
                    print(f"\nMultiple Monarch accounts match group={group}. Pick one:")
                    for i, a in enumerate(exact, start=1):
                        print(f"  {i}) {a.get('displayName')} (id={a.get('id')})")
                    choice = _prompt_choice("Choice: ", min_value=1, max_value=len(exact))
                    acct_id = str(exact[choice - 1].get("id") or "").strip()
                else:
                    raise RuntimeError(
                        f"Multiple Monarch manual accounts matched group={group!r}. "
                        "Run `studentaid_monarch_sync setup-monarch-accounts` to choose the correct account."
                    )
            else:
                token_matches = [
                    a for a in manual_accounts if name_contains_group_token(str(a.get("displayName") or ""), group=group)
                ]
                if len(token_matches) == 1:
                    acct_id = str(token_matches[0].get("id") or "").strip()
                    logger.info(
                        "Auto-mapped group=%s to Monarch manual account by token match: %s",
                        group,
                        str(token_matches[0].get("displayName") or ""),
                    )
                elif len(token_matches) > 1:
                    if yes:
                        acct_id = str(token_matches[0].get("id") or "").strip()
                        logger.warning(
                            "Multiple Monarch manual accounts contained group token %s; choosing the first (%s).",
                            group,
                            str(token_matches[0].get("displayName") or ""),
                        )
                    elif interactive and sys.stdin.isatty():
                        print(f"\nMultiple Monarch accounts contain group token {group}. Pick one:")
                        for i, a in enumerate(token_matches, start=1):
                            print(f"  {i}) {a.get('displayName')} (id={a.get('id')})")
                        choice = _prompt_choice("Choice: ", min_value=1, max_value=len(token_matches))
                        acct_id = str(token_matches[choice - 1].get("id") or "").strip()
                    else:
                        raise RuntimeError(
                            f"Multiple Monarch manual accounts contain group token {group!r}. "
                            "Run `studentaid_monarch_sync setup-monarch-accounts` to choose the correct account."
                        )

        # Still missing? Create (if allowed) or fail.
        if not acct_id:
            if allow_create:
                acct_name = render_loan_account_name(
                    template,
                    group=group,
                    provider=provider,
                    provider_display=provider_display,
                )
                if interactive and sys.stdin.isatty() and not yes:
                    resp = input(f"Create a new Monarch manual account named '{acct_name}' for group {group}? [Y/n] ").strip()
                    if resp.lower().startswith("n"):
                        missing.append(group)
                        continue

                acct_id = await mc.create_student_loan_manual_account(account_name=acct_name, include_in_net_worth=True)
                await _refresh_accounts()
                logger.info("Created Monarch manual account for group=%s: %s (id=%s)", group, acct_name, acct_id)
            else:
                missing.append(group)
                continue

        # Validate that the account exists in Monarch (and refresh once if needed).
        if acct_id not in account_by_id:
            await _refresh_accounts()
        if acct_id not in account_by_id:
            raise RuntimeError(f"Monarch account id not found (group={group}): {acct_id}")

        group_to_account_id[group] = acct_id

        # Update mapping file with stable ID + current displayName (rename-safe).
        dn = str((account_by_id.get(acct_id) or {}).get("displayName") or "").strip()
        prev = mapping.get(group)
        if (prev is None) or (prev.account_id != acct_id) or (dn and prev.account_name != dn):
            mapping[group] = LoanAccountMapping(account_id=acct_id, account_name=dn)
            changed = True

    if missing:
        raise RuntimeError(
            "Missing Monarch account mappings for loan groups: "
            + ", ".join(missing)
            + ". Run `studentaid_monarch_sync setup-monarch-accounts --apply` to auto-create/map accounts, "
            "or create the manual accounts in Monarch and use a naming template like 'Federal-{group}'."
        )

    if changed:
        save_loan_account_mapping(mapping_path, provider=provider, name_template=template, groups=mapping)
        logger.info("Wrote Monarch loan-account mapping: %s", mapping_path)

    return group_to_account_id


async def _setup_monarch_accounts(
    cfg,
    *,
    apply: bool,
    yes: bool,
    no_create: bool,
    name_template: str,
    out_path: str,
) -> None:
    """
    Interactive-ish setup command to map loan groups to Monarch manual accounts, optionally creating them.
    """
    groups = [normalize_group(m.group) for m in cfg.loans if normalize_group(m.group)]
    if not groups:
        raise SystemExit("No loans configured. Set LOAN_GROUPS in .env (recommended), or add loan groups under 'loans:' in a YAML config.")

    provider = (cfg.servicer.provider or "servicer").strip().lower()
    mapping_path = Path(out_path) if out_path else default_mapping_path(provider)

    template = (name_template or cfg.monarch.loan_account_name_template or DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE).strip()
    if not template:
        template = DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE

    mc = MonarchClient(
        email=cfg.monarch.email,
        password=cfg.monarch.password,
        token=cfg.monarch.token,
        mfa_secret=cfg.monarch.mfa_secret,
        session_file=cfg.monarch.session_file,
    )
    await mc.login()

    if not apply:
        # Preview mode: show what we'd do, but don't create anything.
        accounts = await mc.list_accounts()
        manual_accounts = [a for a in accounts if a.get("isManual")]
        provider_display = _servicer_display_name(provider) or provider
        existing = load_loan_account_mapping(mapping_path)

        print(f"Mapping preview (provider={provider!r}) → {mapping_path}")
        print("Loan groups:", ", ".join(groups))
        print()

        for g in groups:
            if g in existing:
                print(f"- {g}: mapped (id={existing[g].account_id}, name={existing[g].account_name!r})")
                continue

            wanted = candidate_loan_account_names(
                template=template, group=g, provider=provider, provider_display=provider_display
            )
            exact = find_exact_name_matches(manual_accounts, wanted)
            if len(exact) == 1:
                print(f"- {g}: would map to existing account {exact[0].get('displayName')!r} (id={exact[0].get('id')})")
                continue
            if len(exact) > 1:
                print(f"- {g}: multiple matches; run with --apply (interactive) to choose")
                continue

            create_name = render_loan_account_name(template, group=g, provider=provider, provider_display=provider_display)
            if no_create:
                print(f"- {g}: no match; would FAIL (creation disabled). Expected name like {create_name!r}")
            else:
                print(f"- {g}: no match; would create new manual account named {create_name!r}")

        print()
        print("To apply (create/map + write mapping file):")
        print(f"  studentaid_monarch_sync setup-monarch-accounts --apply --out {mapping_path}")
        return

    # Apply mode: map accounts, optionally creating them.
    _ = await _resolve_monarch_loan_group_accounts(
        cfg,
        mc,
        allow_create=not no_create,
        yes=yes,
        interactive=not yes,
        name_template_override=template,
        mapping_path_override=str(mapping_path),
    )

    print(f"✅ Monarch loan accounts mapped. Mapping file: {mapping_path}")


async def _apply_monarch_updates(
    cfg,
    state: StateStore,
    loan_snapshots,
    payment_allocations,
    *,
    auto_setup_accounts: bool = False,
) -> None:
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
    group_to_account_id = await _resolve_monarch_loan_group_accounts(
        cfg,
        mc,
        allow_create=bool(auto_setup_accounts or getattr(cfg.monarch, "auto_create_loan_accounts", False)),
        yes=False,
        interactive=False,
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
    provider_disp = _servicer_display_name((cfg.servicer.provider or "").strip().lower()) or "Servicer"
    merchant_name = cfg.monarch.payment_merchant_name or "Student Loan Payment"
    for alloc in payment_allocations:
        acct_id = group_to_account_id.get(alloc.group)
        if not acct_id:
            logger.warning("No Monarch mapping for group=%s; skipping payment txn", alloc.group)
            continue

        key = alloc.allocation_key()
        if state.has_processed_payment(key):
            continue

        ref_part = f" Ref={alloc.payment_reference}" if alloc.payment_reference else ""
        memo = (
            f"{provider_disp} payment allocation.{ref_part} TotalPayment={cents_to_money_str(alloc.payment_total_cents)} "
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
            search=(alloc.payment_reference or "").strip()
            if getattr(cfg.monarch, "duplicate_guard_use_reference", False)
            else "",
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


