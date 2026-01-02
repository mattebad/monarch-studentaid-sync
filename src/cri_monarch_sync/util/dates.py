from __future__ import annotations

from datetime import date

from dateutil import parser as date_parser


def parse_us_date(value: str) -> date:
    """
    Parse dates like:
    - "12/26/2025"
    - "11/26/2025"
    - "12/18/2025"
    """
    if value is None:
        raise ValueError("parse_us_date: value is None")
    s = value.strip()
    if not s:
        raise ValueError("parse_us_date: empty string")
    dt = date_parser.parse(s, dayfirst=False, yearfirst=False)
    return dt.date()


