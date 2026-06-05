import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR


class ProcessTracker:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = BASE_DIR / "outputs" / "tracker.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_files (
                filename TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                total_rows INTEGER DEFAULT 0,
                ok_rows INTEGER DEFAULT 0,
                failed_rows INTEGER DEFAULT 0,
                error TEXT,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS processed_rows (
                filename TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                booking_reference TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                processed_at TEXT,
                PRIMARY KEY (filename, row_index)
            );

            CREATE INDEX IF NOT EXISTS idx_rows_status ON processed_rows(filename, status);
        """)
        self._conn.commit()

    @staticmethod
    def file_hash(filepath: str | Path) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── File tracking ──

    def get_file_status(self, filename: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM processed_files WHERE filename = ?", (filename,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def mark_file_pending(self, filename: str, file_hash: str, total_rows: int):
        self._conn.execute(
            "INSERT OR REPLACE INTO processed_files (filename, file_hash, status, total_rows, started_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (filename, file_hash, total_rows, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        self._conn.commit()

    def mark_file_processing(self, filename: str):
        self._conn.execute(
            "UPDATE processed_files SET status = 'processing', started_at = ? WHERE filename = ?",
            (time.strftime("%Y-%m-%d %H:%M:%S"), filename),
        )
        self._conn.commit()

    def mark_file_completed(self, filename: str, error: str | None = None):
        ok = self._conn.execute(
            "SELECT COUNT(*) FROM processed_rows WHERE filename = ? AND status = 'ok'",
            (filename,),
        ).fetchone()[0]
        failed = self._conn.execute(
            "SELECT COUNT(*) FROM processed_rows WHERE filename = ? AND status = 'failed'",
            (filename,),
        ).fetchone()[0]
        status = "failed" if error else "completed"
        self._conn.execute(
            "UPDATE processed_files SET status = ?, ok_rows = ?, failed_rows = ?, error = ?, finished_at = ? "
            "WHERE filename = ?",
            (status, ok, failed, error, time.strftime("%Y-%m-%d %H:%M:%S"), filename),
        )
        self._conn.commit()

    def is_file_pending(self, filename: str, file_hash: str) -> bool:
        row = self.get_file_status(filename)
        if row is None:
            return True
        if row["status"] == "completed":
            return False
        if row["file_hash"] != file_hash:
            return True
        return row["status"] in ("pending", "processing", "failed")

    def find_pending_files(self, input_dir: str | Path) -> list[Path]:
        files = sorted(Path(input_dir).glob("*.xlsx"))
        result = []
        for f in files:
            h = self.file_hash(f)
            if self.is_file_pending(f.name, h):
                result.append(f)
        return result

    # ── Row tracking ──

    def init_rows(self, filename: str, rows: list[dict[str, Any]]):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        data = [
            (filename, i, r.get("Booking_Reference", ""), "pending", None, now)
            for i, r in enumerate(rows)
        ]
        self._conn.executemany(
            "INSERT OR IGNORE INTO processed_rows (filename, row_index, booking_reference, status, error, processed_at) "
            "VALUES (?, ?, ?, 'pending', NULL, ?)",
            data,
        )
        self._conn.commit()

    def get_pending_rows(self, filename: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM processed_rows WHERE filename = ? AND status = 'pending' ORDER BY row_index",
            (filename,),
        )
        return cur.fetchall()

    def mark_row_processing(self, filename: str, row_index: int):
        self._conn.execute(
            "UPDATE processed_rows SET status = 'processing' WHERE filename = ? AND row_index = ?",
            (filename, row_index),
        )
        self._conn.commit()

    def mark_row_ok(self, filename: str, row_index: int):
        self._conn.execute(
            "UPDATE processed_rows SET status = 'ok', processed_at = ? WHERE filename = ? AND row_index = ?",
            (time.strftime("%Y-%m-%d %H:%M:%S"), filename, row_index),
        )
        self._conn.commit()

    def mark_row_failed(self, filename: str, row_index: int, error: str):
        self._conn.execute(
            "UPDATE processed_rows SET status = 'failed', error = ?, processed_at = ? WHERE filename = ? AND row_index = ?",
            (error, time.strftime("%Y-%m-%d %H:%M:%S"), filename, row_index),
        )
        self._conn.commit()

    def get_row(self, filename: str, row_index: int) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM processed_rows WHERE filename = ? AND row_index = ?",
            (filename, row_index),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ── Management ──

    def get_summary(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT filename, status, total_rows, ok_rows, failed_rows, error, started_at, finished_at "
            "FROM processed_files ORDER BY started_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def reset_file(self, filename: str):
        self._conn.execute("DELETE FROM processed_rows WHERE filename = ?", (filename,))
        self._conn.execute("DELETE FROM processed_files WHERE filename = ?", (filename,))
        self._conn.commit()

    def reset_all(self):
        self._conn.execute("DELETE FROM processed_rows")
        self._conn.execute("DELETE FROM processed_files")
        self._conn.commit()

    def close(self):
        self._conn.close()
