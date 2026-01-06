#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _split_groups(s: str) -> list[str]:
    items = re.split(r"[,\s]+", (s or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        g = (item or "").strip().upper()
        if not g or g in seen:
            continue
        out.append(g)
        seen.add(g)
    return out


def _read_text(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    return p.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _ensure_src_on_path()

    from studentaid_monarch_sync.portal.client import PortalCredentials, ServicerPortalClient

    p = argparse.ArgumentParser(
        prog="parse_portal_text_snapshot",
        description=(
            "Parse Playwright-saved portal text snapshots (from data/debug/*.txt) into structured JSON.\n"
            "This is intended for debugging parsing regressions offline (no Playwright, no secrets)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    loans = sub.add_parser("loans", help="Parse a My Loans text snapshot into LoanSnapshot[]")
    loans.add_argument("--groups", required=True, help="Comma/space separated loan groups (e.g. AA,AB,1-01)")
    loans.add_argument("--file", required=True, help="Path to a debug .txt file captured from the loan-details page")
    loans.add_argument("--out", default="", help="Optional output JSON path (otherwise prints to stdout)")

    payments = sub.add_parser("payments", help="Parse a payment detail text snapshot into PaymentAllocation[]")
    payments.add_argument("--file", required=True, help="Path to a debug .txt file captured from a payment detail page")
    payments.add_argument("--out", default="", help="Optional output JSON path (otherwise prints to stdout)")

    args = p.parse_args(argv)

    # Construct a client only to reuse its parsing helpers. No navigation is performed.
    client = ServicerPortalClient(
        base_url="https://example.studentaid.gov",
        creds=PortalCredentials(username="x", password="x"),
    )

    if args.cmd == "loans":
        body_text = _read_text(args.file)
        groups = _split_groups(args.groups)
        if not groups:
            raise SystemExit("No groups provided.")

        snaps = []
        for g in groups:
            section = client._extract_group_section_text(body_text, group=g)
            snap = client._parse_loan_snapshot(group=g, body_text=section)
            snaps.append(snap.model_dump())

        payload = {"loan_snapshots": snaps}
        out_json = json.dumps(payload, indent=2, sort_keys=False)
        if args.out:
            Path(args.out).write_text(out_json, encoding="utf-8")
        else:
            print(out_json)
        return 0

    if args.cmd == "payments":
        body_text = _read_text(args.file)
        allocs = client._parse_payment_allocations(body_text)
        payload = {"payment_allocations": [a.model_dump() for a in allocs]}
        out_json = json.dumps(payload, indent=2, sort_keys=False)
        if args.out:
            Path(args.out).write_text(out_json, encoding="utf-8")
        else:
            print(out_json)
        return 0

    raise AssertionError("Unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())


