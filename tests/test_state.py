from __future__ import annotations

from pathlib import Path

from cri_monarch_sync.state import StateStore


def test_state_creates_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    s = StateStore(str(db_path))
    try:
        rid = s.record_run_start()
        s.record_run_finish(rid, ok=True, message="test")
    finally:
        s.close()

    bak = tmp_path / "state.db.bak"
    assert db_path.exists()
    assert bak.exists()
    assert bak.stat().st_size > 0


def test_state_restores_from_backup_when_db_corrupted(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"

    # Create a valid DB + backup.
    s1 = StateStore(str(db_path))
    try:
        rid = s1.record_run_start()
        s1.record_run_finish(rid, ok=True, message="test")
    finally:
        s1.close()

    bak = tmp_path / "state.db.bak"
    assert bak.exists()

    # Corrupt the main DB file.
    db_path.write_bytes(b"not a sqlite db")

    # Re-open: should quarantine the corrupted DB and restore from backup.
    s2 = StateStore(str(db_path))
    try:
        rid2 = s2.record_run_start()
        s2.record_run_finish(rid2, ok=True, message="after-restore")
        assert s2.has_processed_payment("nope") is False
    finally:
        s2.close()

    # We should have created a quarantined file.
    quarantined = list(tmp_path.glob("state.db.corrupt-*"))
    assert quarantined, "expected quarantined corrupted db file to be created"


