from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Iterable, Optional


def create_debug_bundle(
    *,
    debug_dir: str,
    log_file: str,
    out_dir: str = "data",
    provider: str = "",
    extra_paths: Optional[Iterable[str]] = None,
) -> Path:
    """
    Create a shareable zip containing debug artifacts + logs.

    Intentionally excludes secrets (e.g., .env, config.yaml, storage_state files).
    """
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    prov = (provider or "").strip().lower()
    prov_part = f"_{prov}" if prov else ""
    out_path = out_root / f"debug_bundle{prov_part}_{stamp}.zip"

    dbg = Path(debug_dir)
    log = Path(log_file)

    def _add_file(z: zipfile.ZipFile, file_path: Path, arcname: str) -> None:
        try:
            if file_path.exists() and file_path.is_file():
                z.write(file_path, arcname=arcname)
        except Exception:
            # best-effort; don't fail bundling because a file disappeared
            return

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        _add_file(z, log, arcname=log.name)

        if dbg.exists() and dbg.is_dir():
            for p in sorted(dbg.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(dbg)
                _add_file(z, p, arcname=str(Path("debug") / rel))

        if extra_paths:
            for raw in extra_paths:
                p = Path(raw)
                if not p.exists():
                    continue
                if p.is_file():
                    _add_file(z, p, arcname=str(Path("extra") / p.name))
                elif p.is_dir():
                    for f in sorted(p.rglob("*")):
                        if f.is_file():
                            rel = f.relative_to(p)
                            _add_file(z, f, arcname=str(Path("extra") / p.name / rel))

    return out_path


