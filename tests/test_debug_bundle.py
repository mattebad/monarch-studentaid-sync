from __future__ import annotations

import zipfile
from pathlib import Path

from studentaid_monarch_sync.util.debug_bundle import create_debug_bundle


def test_create_debug_bundle_includes_debug_and_log(tmp_path: Path) -> None:
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    (debug_dir / "step_01.png").write_bytes(b"png")
    (debug_dir / "nav_failed.html").write_text("<html/>", encoding="utf-8")

    log_file = tmp_path / "sync.log"
    log_file.write_text("hello", encoding="utf-8")

    out = create_debug_bundle(
        debug_dir=str(debug_dir),
        log_file=str(log_file),
        out_dir=str(tmp_path),
        provider="nelnet",
    )
    assert out.exists()
    assert out.suffix == ".zip"

    with zipfile.ZipFile(out, "r") as z:
        names = set(z.namelist())
        assert "sync.log" in names
        assert "debug/step_01.png" in names
        assert "debug/nav_failed.html" in names


