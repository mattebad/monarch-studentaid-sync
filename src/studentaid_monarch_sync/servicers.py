from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ServicerInfo:
    provider: str
    display_name: str


# Convenience registry of common StudentAid servicer subdomains.
# This list is intentionally non-exhaustive; unknown providers are still allowed as long as the URL works.
KNOWN_SERVICERS: Mapping[str, ServicerInfo] = {
    # Common current servicers (subdomain pattern: https://{provider}.studentaid.gov)
    "aidvantage": ServicerInfo(provider="aidvantage", display_name="Aidvantage"),
    "cri": ServicerInfo(provider="cri", display_name="Central Research, Inc."),
    "edfinancial": ServicerInfo(provider="edfinancial", display_name="Edfinancial"),
    "mohela": ServicerInfo(provider="mohela", display_name="MOHELA"),
    "nelnet": ServicerInfo(provider="nelnet", display_name="Nelnet"),
}


def is_known_provider(provider: str) -> bool:
    return provider.strip().lower() in KNOWN_SERVICERS


