from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessedPayment:
    key: str
    payment_date: str
    group_code: str
    total_applied_cents: int
    payment_total_cents: int
    monarch_transaction_id: Optional[str]
    created_at: str


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_path = self.db_path.with_name(self.db_path.name + ".bak")

        # Self-heal on corrupted/missing DB: restore from backup when possible.
        self._conn = self._open_or_restore()
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._ensure_schema()

        # Ensure we have *some* backup available for next time.
        self._maybe_backup(if_missing=True)

    def close(self) -> None:
        self._conn.close()

    def _open_or_restore(self) -> sqlite3.Connection:
        """
        Open the state DB. If it looks corrupted, move it aside and restore from the last-known-good backup.

        Note: Even if we cannot restore, the app can still run safely because we also have a Monarch-side
        duplicate guard. This DB is primarily for idempotency + speed.
        """
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                if self._connection_is_healthy(conn):
                    return conn
                conn.close()
                raise sqlite3.DatabaseError("SQLite quick_check failed")
            except Exception as e:
                logger.warning("State DB appears corrupted/unreadable; attempting restore from backup. (%s)", e)
                self._quarantine_db_files()

                # Restore from backup if we have one.
                if self._backup_path.exists():
                    try:
                        shutil.copy2(self._backup_path, self.db_path)
                        conn = sqlite3.connect(self.db_path)
                        if self._connection_is_healthy(conn):
                            logger.warning("Restored state DB from backup: %s", self._backup_path)
                            return conn
                        conn.close()
                    except Exception:
                        logger.warning("Failed to restore state DB from backup; creating a fresh DB.", exc_info=True)
                else:
                    logger.warning("No state DB backup found; creating a fresh DB.")

        # Fresh DB (either first run or restore failed).
        return sqlite3.connect(self.db_path)

    def _connection_is_healthy(self, conn: sqlite3.Connection) -> bool:
        """
        Best-effort sanity check: confirm the file is a readable SQLite DB and passes quick_check.
        """
        try:
            # Touch schema_version to fail fast on "file is not a database".
            _ = conn.execute("PRAGMA schema_version;").fetchone()
            row = conn.execute("PRAGMA quick_check;").fetchone()
            return bool(row and row[0] == "ok")
        except Exception:
            return False

    def _quarantine_db_files(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for p in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                if p.exists():
                    p.replace(p.with_name(p.name + f".corrupt-{stamp}"))
            except Exception:
                logger.debug("Failed to quarantine path=%s", p, exc_info=True)

    def _maybe_backup(self, *, if_missing: bool) -> None:
        # If we're only creating a backup when missing and one already exists, do nothing.
        if if_missing and self._backup_path.exists():
            return

        try:
            self.backup()
        except Exception:
            logger.debug("Failed to write state DB backup.", exc_info=True)

    def backup(self) -> None:
        """
        Write/refresh a last-known-good backup of the state DB at `<db_path>.bak`.
        """
        out = self._backup_path
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

        # Use SQLite online backup API for a consistent snapshot.
        dst = sqlite3.connect(tmp)
        try:
            self._conn.backup(dst)
            dst.commit()
        finally:
            dst.close()

        tmp.replace(out)

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_payment_allocations (
              key TEXT PRIMARY KEY,
              payment_date TEXT NOT NULL,
              group_code TEXT NOT NULL,
              total_applied_cents INTEGER NOT NULL,
              payment_total_cents INTEGER NOT NULL,
              monarch_transaction_id TEXT,
              created_at TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_updates (
              group_code TEXT PRIMARY KEY,
              last_balance_date TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              ok INTEGER,
              message TEXT
            );
            """
        )
        self._apply_light_migrations()
        self._conn.commit()

    def _apply_light_migrations(self) -> None:
        # Best-effort schema evolution for early versions.
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(processed_payment_allocations);").fetchall()}
        if "total_applied_cents" not in cols:
            self._conn.execute("ALTER TABLE processed_payment_allocations ADD COLUMN total_applied_cents INTEGER;")
        if "payment_total_cents" not in cols:
            self._conn.execute("ALTER TABLE processed_payment_allocations ADD COLUMN payment_total_cents INTEGER;")
        if "monarch_transaction_id" not in cols:
            self._conn.execute("ALTER TABLE processed_payment_allocations ADD COLUMN monarch_transaction_id TEXT;")

    def has_processed_payment(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_payment_allocations WHERE key = ? LIMIT 1;",
            (key,),
        ).fetchone()
        return row is not None

    def mark_processed_payment(
        self,
        *,
        key: str,
        payment_date: date,
        group_code: str,
        total_applied_cents: int,
        payment_total_cents: int,
        monarch_transaction_id: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_payment_allocations(
              key, payment_date, group_code, total_applied_cents, payment_total_cents, monarch_transaction_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                key,
                payment_date.isoformat(),
                group_code,
                total_applied_cents,
                payment_total_cents,
                monarch_transaction_id,
                now,
            ),
        )
        self._conn.commit()

    def record_run_start(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute("INSERT INTO runs(started_at) VALUES (?);", (now,))
        self._conn.commit()
        return int(cur.lastrowid)

    def record_run_finish(self, run_id: int, *, ok: bool, message: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, message = ? WHERE id = ?;",
            (now, 1 if ok else 0, message, run_id),
        )
        self._conn.commit()

        # Only refresh backups after a successful run finish (avoid snapshotting a potentially bad state).
        if ok:
            # Refresh the backup on successful runs so we always have a recent last-known-good snapshot.
            self._maybe_backup(if_missing=False)

    def get_last_balance_date(self, group_code: str) -> Optional[date]:
        row = self._conn.execute(
            "SELECT last_balance_date FROM balance_updates WHERE group_code = ?;",
            (group_code,),
        ).fetchone()
        if not row:
            return None
        return date.fromisoformat(row[0])

    def set_last_balance_date(self, group_code: str, balance_date: date) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO balance_updates(group_code, last_balance_date, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_code) DO UPDATE SET
              last_balance_date = excluded.last_balance_date,
              updated_at = excluded.updated_at;
            """,
            (group_code, balance_date.isoformat(), now, now),
        )
        self._conn.commit()


