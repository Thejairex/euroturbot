import hashlib
import time
from pathlib import Path
from typing import Any

from config.settings import (
    BASE_DIR,
    DB_CONNECTION,
    DB_HOST,
    DB_PORT,
    DB_DATABASE,
    DB_USERNAME,
    DB_PASSWORD,
)


class ProcessTracker:
    def __init__(self, db_path: str | Path | None = None):
        self._db_type = "pgsql" if DB_CONNECTION == "pgsql" else "sqlite"

        if self._db_type == "pgsql":
            import psycopg2
            import psycopg2.extras

            self._conn = psycopg2.connect(
                host=DB_HOST,
                port=int(DB_PORT),
                dbname=DB_DATABASE,
                user=DB_USERNAME,
                password=DB_PASSWORD,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            self._conn.autocommit = False
        else:
            import sqlite3

            if db_path is None:
                db_path = BASE_DIR / "outputs" / "tracker.db"
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row

        self._init_db()

    # ── Helpers de ejecución ──────────────────────────────────────────────────

    def _execute(self, sql: str, params=None):
        """Ejecuta SQL usando %s como placeholder (se convierte a ? en SQLite)."""
        if self._db_type == "sqlite":
            return self._conn.execute(sql.replace("%s", "?"), params or ())
        cur = self._conn.cursor()
        cur.execute(sql, params or ())
        return cur

    def _executemany(self, sql: str, data):
        if self._db_type == "sqlite":
            return self._conn.executemany(sql.replace("%s", "?"), data)
        cur = self._conn.cursor()
        cur.executemany(sql, data)
        return cur

    def _fetchone(self, sql: str, params=None):
        return self._execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params=None):
        return self._execute(sql, params).fetchall()

    # ── Inicialización del esquema ────────────────────────────────────────────

    def _init_db(self):
        stmts = [
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_rows (
                filename TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                booking_reference TEXT,
                supplier_code TEXT,
                currency TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                processed_at TEXT,
                PRIMARY KEY (filename, row_index)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rows_status ON processed_rows(filename, status)",
        ]

        if self._db_type == "pgsql":
            cur = self._conn.cursor()
            for s in stmts:
                cur.execute(s)

            # Si las tablas ya existían sin PK (ej. creadas manualmente), las agrega.
            # La lógica condicional queda en PL/pgSQL para evitar fetches en Python.
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_schema = 'public'
                          AND table_name = 'processed_files'
                          AND constraint_type = 'PRIMARY KEY'
                    ) THEN
                        ALTER TABLE processed_files ADD PRIMARY KEY (filename);
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_schema = 'public'
                          AND table_name = 'processed_rows'
                          AND constraint_type = 'PRIMARY KEY'
                    ) THEN
                        ALTER TABLE processed_rows ADD PRIMARY KEY (filename, row_index);
                    END IF;
                END $$;
            """)

            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'processed_rows' AND table_schema = 'public'"
            )
            existing = {r["column_name"] for r in cur.fetchall()}
        else:
            for s in stmts:
                self._conn.execute(s)
            existing = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(processed_rows)")
            }

        for col in ("supplier_code", "currency", "transaction_reference"):
            if col not in existing:
                self._execute(f"ALTER TABLE processed_rows ADD COLUMN {col} TEXT")

        self._conn.commit()

    # ── Utilidades ────────────────────────────────────────────────────────────

    @staticmethod
    def file_hash(filepath: str | Path) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── Tracking de archivos ──────────────────────────────────────────────────

    def get_file_status(self, filename: str) -> dict | None:
        row = self._fetchone(
            "SELECT * FROM processed_files WHERE filename = %s", (filename,)
        )
        return dict(row) if row else None

    def mark_file_pending(self, filename: str, file_hash: str, total_rows: int):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._db_type == "pgsql":
            self._execute(
                "INSERT INTO processed_files "
                "(filename, file_hash, status, total_rows, started_at) "
                "VALUES (%s, %s, 'pending', %s, %s) "
                "ON CONFLICT (filename) DO UPDATE SET "
                "file_hash = EXCLUDED.file_hash, status = 'pending', "
                "total_rows = EXCLUDED.total_rows, started_at = EXCLUDED.started_at",
                (filename, file_hash, total_rows, now),
            )
        else:
            self._execute(
                "INSERT OR REPLACE INTO processed_files "
                "(filename, file_hash, status, total_rows, started_at) "
                "VALUES (%s, %s, 'pending', %s, %s)",
                (filename, file_hash, total_rows, now),
            )
        self._conn.commit()

    def mark_file_processing(self, filename: str):
        self._execute(
            "UPDATE processed_files SET status = 'processing', started_at = %s "
            "WHERE filename = %s",
            (time.strftime("%Y-%m-%d %H:%M:%S"), filename),
        )
        self._conn.commit()

    def mark_file_completed(self, filename: str, error: str | None = None):
        ok = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM processed_rows "
            "WHERE filename = %s AND status = 'ok'",
            (filename,),
        )["cnt"]
        failed = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM processed_rows "
            "WHERE filename = %s AND status = 'failed'",
            (filename,),
        )["cnt"]
        status = "failed" if error else "completed"
        self._execute(
            "UPDATE processed_files SET status = %s, ok_rows = %s, failed_rows = %s, "
            "error = %s, finished_at = %s WHERE filename = %s",
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

    # ── Tracking de filas ─────────────────────────────────────────────────────

    def init_rows(self, filename: str, rows: list[dict[str, Any]]):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        data = [
            (
                filename,
                i,
                r.get("Voucher_Number", ""),
                (r.get("Supplier_Code") or "").strip(),
                (r.get("Service_Cost_Currency") or "").strip(),
                "pending",
                None,
                now,
            )
            for i, r in enumerate(rows)
        ]
        if self._db_type == "pgsql":
            self._executemany(
                "INSERT INTO processed_rows "
                "(filename, row_index, booking_reference, supplier_code, currency, "
                "status, error, processed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (filename, row_index) DO NOTHING",
                data,
            )
        else:
            self._executemany(
                "INSERT OR IGNORE INTO processed_rows "
                "(filename, row_index, booking_reference, supplier_code, currency, "
                "status, error, processed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                data,
            )
        self._conn.commit()

    def get_pending_rows(self, filename: str) -> list:
        return self._fetchall(
            "SELECT * FROM processed_rows WHERE filename = %s AND status = 'pending' "
            "ORDER BY row_index",
            (filename,),
        )

    def mark_row_processing(self, filename: str, row_index: int):
        self._execute(
            "UPDATE processed_rows SET status = 'processing' "
            "WHERE filename = %s AND row_index = %s",
            (filename, row_index),
        )
        self._conn.commit()

    def mark_row_ok(self, filename: str, row_index: int):
        self._execute(
            "UPDATE processed_rows SET status = 'ok', processed_at = %s "
            "WHERE filename = %s AND row_index = %s",
            (time.strftime("%Y-%m-%d %H:%M:%S"), filename, row_index),
        )
        self._conn.commit()

    def mark_row_failed(self, filename: str, row_index: int, error: str):
        self._execute(
            "UPDATE processed_rows SET status = 'failed', error = %s, processed_at = %s "
            "WHERE filename = %s AND row_index = %s",
            (error, time.strftime("%Y-%m-%d %H:%M:%S"), filename, row_index),
        )
        self._conn.commit()

    def mark_row_skipped(self, filename: str, row_index: int):
        self._execute(
            "UPDATE processed_rows SET status = 'skipped', processed_at = %s "
            "WHERE filename = %s AND row_index = %s",
            (time.strftime("%Y-%m-%d %H:%M:%S"), filename, row_index),
        )
        self._conn.commit()

    def mark_rows_ok_bulk(self, filename: str, row_indices: list[int], reference: str | None = None):
        if not row_indices:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if reference:
            self._executemany(
                "UPDATE processed_rows SET status = 'ok', processed_at = %s, transaction_reference = %s "
                "WHERE filename = %s AND row_index = %s",
                [(now, reference, filename, idx) for idx in row_indices],
            )
        else:
            self._executemany(
                "UPDATE processed_rows SET status = 'ok', processed_at = %s "
                "WHERE filename = %s AND row_index = %s",
                [(now, filename, idx) for idx in row_indices],
            )
        self._conn.commit()

    def mark_rows_failed_bulk(self, filename: str, row_indices: list[int], error: str):
        if not row_indices:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self._executemany(
            "UPDATE processed_rows SET status = 'failed', error = %s, processed_at = %s "
            "WHERE filename = %s AND row_index = %s",
            [(error, now, filename, idx) for idx in row_indices],
        )
        self._conn.commit()

    def mark_rows_skipped_bulk(self, filename: str, row_indices: list[int]):
        if not row_indices:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self._executemany(
            "UPDATE processed_rows SET status = 'skipped', processed_at = %s "
            "WHERE filename = %s AND row_index = %s",
            [(now, filename, idx) for idx in row_indices],
        )
        self._conn.commit()

    def mark_row_pending(self, filename: str, row_index: int):
        self._execute(
            "UPDATE processed_rows SET status = 'pending', error = NULL "
            "WHERE filename = %s AND row_index = %s",
            (filename, row_index),
        )
        self._conn.commit()

    def reset_processing_to_pending(self, filename: str) -> int:
        cur = self._execute(
            "UPDATE processed_rows SET status = 'pending' "
            "WHERE filename = %s AND status = 'processing'",
            (filename,),
        )
        self._conn.commit()
        return cur.rowcount

    def reset_failed_to_pending(self, filename: str) -> int:
        """Vuelve a 'pending' las filas 'failed' para reintentarlas (1 vez por ejecución)."""
        cur = self._execute(
            "UPDATE processed_rows SET status = 'pending', error = NULL "
            "WHERE filename = %s AND status = 'failed'",
            (filename,),
        )
        self._conn.commit()
        return cur.rowcount

    def reset_skipped_to_pending(self, filename: str) -> int:
        """Vuelve a 'pending' las filas 'skipped' para reintentarlas (1 vez por ejecución)."""
        cur = self._execute(
            "UPDATE processed_rows SET status = 'pending', error = NULL "
            "WHERE filename = %s AND status = 'skipped'",
            (filename,),
        )
        self._conn.commit()
        return cur.rowcount

    def count_failed_rows(self, filename: str) -> int:
        """Cuenta las filas en estado 'failed' de un archivo."""
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM processed_rows "
            "WHERE filename = %s AND status = 'failed'",
            (filename,),
        )
        return row["cnt"] if row else 0

    def get_row(self, filename: str, row_index: int) -> dict | None:
        row = self._fetchone(
            "SELECT * FROM processed_rows WHERE filename = %s AND row_index = %s",
            (filename, row_index),
        )
        return dict(row) if row else None

    # ── Gestión general ───────────────────────────────────────────────────────

    def get_summary(self) -> list[dict]:
        rows = self._fetchall(
            "SELECT filename, status, total_rows, ok_rows, failed_rows, error, "
            "started_at, finished_at "
            "FROM processed_files ORDER BY started_at DESC"
        )
        return [dict(r) for r in rows]

    def reset_file(self, filename: str):
        self._execute("DELETE FROM processed_rows WHERE filename = %s", (filename,))
        self._execute("DELETE FROM processed_files WHERE filename = %s", (filename,))
        self._conn.commit()

    def reset_all(self):
        self._execute("DELETE FROM processed_rows")
        self._execute("DELETE FROM processed_files")
        self._conn.commit()

    def close(self):
        self._conn.close()
