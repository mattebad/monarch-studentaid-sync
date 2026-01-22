from __future__ import annotations

import sys
from typing import Any
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "portal: integration smoke tests that require real servicer + Monarch credentials",
    )


