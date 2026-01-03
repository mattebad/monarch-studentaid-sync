from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


_MONEY_RE = re.compile(r"[-+]?\$?\s*[\d,]+(?:\.\d{1,2})?")


def money_to_cents(value: str) -> int:
    """
    Parse values like:
    - "$3,040.16"
    - "3040.16"
    - "$0.37"
    - "-$12.34"
    """
    if value is None:
        raise ValueError("money_to_cents: value is None")

    s = value.strip()
    if not s:
        raise ValueError("money_to_cents: empty string")

    # Remove currency symbols/spaces/commas
    s = s.replace("$", "").replace(",", "").strip()

    # Handle parentheses as negative
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()

    dec = Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(dec * 100)


def find_first_money(text: str) -> Optional[str]:
    m = _MONEY_RE.search(text or "")
    return m.group(0) if m else None


def cents_to_money_str(cents: int) -> str:
    dec = (Decimal(cents) / 100).quantize(Decimal("0.01"))
    return f"${dec:,.2f}"


