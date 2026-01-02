from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE = "{provider}-{group}"

_GROUP_TOKEN_RE = re.compile(r"\b([A-Z]{2})\b")


def normalize_group(group: str) -> str:
    return (group or "").strip().upper()


def _normalize_name(s: str) -> str:
    """
    Normalize account names for fuzzy-ish matching:
    - casefold
    - collapse whitespace
    - treat '-' and '_' as whitespace
    """
    s = (s or "").strip().casefold()
    s = s.replace("-", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def default_mapping_path(provider: str) -> Path:
    slug = (provider or "servicer").strip().lower() or "servicer"
    return Path(f"data/monarch_loan_accounts_{slug}.json")


def render_loan_account_name(
    template: str,
    *,
    group: str,
    provider: str,
    provider_display: str,
) -> str:
    tmpl = (template or DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE).strip()
    g = normalize_group(group)
    provider_slug = (provider or "").strip().lower()
    provider_disp = (provider_display or provider_slug or "Servicer").strip()

    values = {
        "group": g,
        "provider": provider_slug,
        "provider_upper": provider_slug.upper(),
        "provider_display": provider_disp,
    }

    try:
        out = tmpl.format(**values).strip()
        return out or f"{provider_slug}-{g}"
    except Exception:
        # Keep this ultra-defensive: templates are user-input and we don't want setup to crash.
        return f"{provider_slug}-{g}"


def candidate_loan_account_names(
    *,
    template: str,
    group: str,
    provider: str,
    provider_display: str,
) -> list[str]:
    """
    Return a prioritized list of possible Monarch manual account display names that might
    correspond to a given loan group.
    """
    g = normalize_group(group)
    provider_slug = (provider or "").strip().lower()
    provider_disp = (provider_display or provider_slug or "").strip()

    names: list[str] = [
        render_loan_account_name(template, group=g, provider=provider_slug, provider_display=provider_disp),
        f"{provider_slug}-{g}" if provider_slug else "",
        f"{provider_slug.upper()}-{g}" if provider_slug else "",
        f"{provider_disp}-{g}" if provider_disp else "",
        f"Federal-{g}",
        f"Federal {g}",
        f"Student Loan-{g}",
        f"Student Loan {g}",
    ]

    # De-dupe, preserve order.
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        nn = (n or "").strip()
        if not nn:
            continue
        key = _normalize_name(nn)
        if key in seen:
            continue
        out.append(nn)
        seen.add(key)
    return out


def find_exact_name_matches(accounts: Iterable[Dict[str, Any]], wanted_names: Iterable[str]) -> list[Dict[str, Any]]:
    wanted = {_normalize_name(n) for n in wanted_names if (n or "").strip()}
    if not wanted:
        return []
    matches: list[Dict[str, Any]] = []
    for a in accounts:
        dn = str(a.get("displayName") or "")
        if _normalize_name(dn) in wanted:
            matches.append(dict(a))
    return matches


def name_contains_group_token(display_name: str, *, group: str) -> bool:
    """
    Best-effort: does the displayName contain the loan group as a token?
    Examples that match group=AA:
    - "Federal-AA"
    - "Student Loan AA"
    """
    g = normalize_group(group)
    if not g:
        return False
    return bool(re.search(rf"\b{re.escape(g)}\b", (display_name or "").upper()))


def list_group_tokens_in_name(display_name: str) -> list[str]:
    return [m.group(1).upper() for m in _GROUP_TOKEN_RE.finditer(display_name or "")]


@dataclass(frozen=True)
class LoanAccountMapping:
    account_id: str
    account_name: str = ""


def load_loan_account_mapping(path: Path) -> dict[str, LoanAccountMapping]:
    """
    Load a mapping file from disk.
    If the JSON is corrupt, quarantine it as `.bad` and return an empty mapping.
    """
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        groups = raw.get("groups") or {}
        out: dict[str, LoanAccountMapping] = {}
        if isinstance(groups, dict):
            for group, item in groups.items():
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("account_id") or "").strip()
                if not gid:
                    continue
                out[normalize_group(str(group))] = LoanAccountMapping(
                    account_id=gid,
                    account_name=str(item.get("account_name") or "").strip(),
                )
        return out
    except Exception:
        try:
            bad = path.with_suffix(path.suffix + ".bad")
            path.replace(bad)
            logger.warning("Invalid mapping JSON; quarantined to %s", bad)
        except Exception:
            logger.debug("Failed to quarantine invalid mapping JSON.", exc_info=True)
        return {}


def save_loan_account_mapping(
    path: Path,
    *,
    provider: str,
    name_template: str,
    groups: dict[str, LoanAccountMapping],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": 1,
        "provider": (provider or "").strip().lower(),
        "name_template": (name_template or DEFAULT_LOAN_ACCOUNT_NAME_TEMPLATE).strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "groups": {
            normalize_group(k): {"account_id": v.account_id, "account_name": v.account_name}
            for k, v in sorted(groups.items(), key=lambda kv: kv[0])
            if v.account_id
        },
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")


