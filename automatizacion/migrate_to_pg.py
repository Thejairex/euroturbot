"""
Migración SQLite → PostgreSQL para el tracker de EuroTur.

Uso (desde automatizacion/):
    python migrate_to_pg.py                   # solo recrea tablas con PKs correctas
    python migrate_to_pg.py --import-sqlite   # recrea + importa desde outputs/tracker.db
    python migrate_to_pg.py --reset --import-sqlite  # borra todo y reimporta desde cero
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import psycopg2
import psycopg2.extras

from config.settings import BASE_DIR, DB_DATABASE, DB_HOST, DB_PASSWORD, DB_PORT, DB_USERNAME


def get_pg_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        dbname=DB_DATABASE,
        user=DB_USERNAME,
        password=DB_PASSWORD,
    )


def setup_tables(cur, reset: bool = False):
    if reset:
        cur.execute("DROP TABLE IF EXISTS processed_rows CASCADE")
        cur.execute("DROP TABLE IF EXISTS processed_files CASCADE")
        print("  Tablas eliminadas.")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            filename    TEXT PRIMARY KEY,
            file_hash   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            total_rows  INTEGER DEFAULT 0,
            ok_rows     INTEGER DEFAULT 0,
            failed_rows INTEGER DEFAULT 0,
            error       TEXT,
            started_at  TEXT,
            finished_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_rows (
            filename          TEXT NOT NULL,
            row_index         INTEGER NOT NULL,
            booking_reference TEXT,
            supplier_code     TEXT,
            currency          TEXT,
            status            TEXT NOT NULL DEFAULT 'pending',
            error             TEXT,
            processed_at      TEXT,
            PRIMARY KEY (filename, row_index)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_rows_status ON processed_rows(filename, status)"
    )
    print("  Tablas listas con PKs correctas.")


def import_from_sqlite(cur, sqlite_path: Path):
    if not sqlite_path.exists():
        print(f"  {sqlite_path} no encontrado — saltando importación.")
        return

    sq = sqlite3.connect(str(sqlite_path))
    sq.row_factory = sqlite3.Row

    # ── processed_files ───────────────────────────────────────────────────────
    try:
        files = sq.execute("SELECT * FROM processed_files").fetchall()
    except Exception as e:
        print(f"  Advertencia: no se pudo leer processed_files: {e}")
        files = []

    if files:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO processed_files
                (filename, file_hash, status, total_rows, ok_rows,
                 failed_rows, error, started_at, finished_at)
            VALUES %s
            ON CONFLICT (filename) DO UPDATE SET
                file_hash   = EXCLUDED.file_hash,
                status      = EXCLUDED.status,
                total_rows  = EXCLUDED.total_rows,
                ok_rows     = EXCLUDED.ok_rows,
                failed_rows = EXCLUDED.failed_rows,
                error       = EXCLUDED.error,
                started_at  = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at
            """,
            [
                (
                    r["filename"], r["file_hash"], r["status"],
                    r["total_rows"], r["ok_rows"], r["failed_rows"],
                    r["error"], r["started_at"], r["finished_at"],
                )
                for r in files
            ],
        )
        print(f"  processed_files: {len(files)} filas importadas.")

    # ── processed_rows (en chunks para no explotar memoria) ───────────────────
    CHUNK = 5000
    offset = 0
    total = 0
    while True:
        try:
            rows = sq.execute(
                "SELECT * FROM processed_rows LIMIT ? OFFSET ?", (CHUNK, offset)
            ).fetchall()
        except Exception as e:
            print(f"\n  Advertencia: error en SQLite offset {offset}: {e}")
            break
        if not rows:
            break

        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO processed_rows
                (filename, row_index, booking_reference, supplier_code,
                 currency, status, error, processed_at)
            VALUES %s
            ON CONFLICT (filename, row_index) DO NOTHING
            """,
            [
                (
                    r["filename"], r["row_index"], r["booking_reference"],
                    r["supplier_code"], r["currency"],
                    r["status"], r["error"], r["processed_at"],
                )
                for r in rows
            ],
        )
        total += len(rows)
        offset += CHUNK
        print(f"  processed_rows: {total} filas...", end="\r", flush=True)

    sq.close()
    print(f"  processed_rows: {total} filas importadas.          ")


def main():
    parser = argparse.ArgumentParser(description="Migración SQLite → PostgreSQL")
    parser.add_argument(
        "--import-sqlite",
        action="store_true",
        help="Importar datos desde outputs/tracker.db",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Borrar tablas PG antes de recrear (los datos de PG se pierden)",
    )
    args = parser.parse_args()

    print(f"Conectando a PostgreSQL {DB_HOST}:{DB_PORT}/{DB_DATABASE}...")
    conn = get_pg_conn()
    cur = conn.cursor()

    try:
        print("Configurando tablas...")
        setup_tables(cur, reset=args.reset)

        if args.import_sqlite:
            sqlite_path = BASE_DIR / "outputs" / "tracker.db"
            print(f"Importando desde {sqlite_path}...")
            import_from_sqlite(cur, sqlite_path)

        conn.commit()
        print("✓ Completado.")
    except Exception as e:
        conn.rollback()
        print(f"✗ Error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
