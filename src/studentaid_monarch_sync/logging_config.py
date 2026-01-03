import logging
import os
from pathlib import Path
from typing import Optional


def configure_logging(level: str = "INFO", file_path: Optional[str] = None) -> None:
    numeric_level = getattr(logging, (level or "INFO").upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if file_path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
        force=True,  # allow configure_logging() to be called multiple times (CLI does this)
    )

    # Reduce noise from chatty libraries
    for noisy in ("playwright", "urllib3"):
        logging.getLogger(noisy).setLevel(os.getenv("NOISY_LOG_LEVEL", "WARNING"))


