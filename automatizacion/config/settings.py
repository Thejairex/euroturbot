import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

ENV = {
    "USERNAME": os.getenv("LOGIN_USERNAME", ""),
    "PASSWORD": os.getenv("LOGIN_PASSWORD", ""),
    "URL": os.getenv("LOGIN_URL", ""),
    "LOGIN_PATH": "#/login",
    "CREDITOR_PATH": "#/creditor",
}

DEFAULT_TIMEOUT = 30000
NAVIGATION_TIMEOUT = 60000
POLLING_INTERVAL = 500

CACHE_ENABLED = True
CACHE_TTL_SECONDS = 300
CACHE_DISK_ENABLED = True
CACHE_DISK_DIR = BASE_DIR / "outputs" / "cache"

HEADLESS_DEFAULT = False

SCREENSHOT_DIR = BASE_DIR / "outputs" / "screenshots"
LOG_DIR = BASE_DIR / "outputs" / "logs"
REPORT_DIR = BASE_DIR / "outputs" / "reports"

SCREENSHOT_ON_ERROR = True
MAX_RETRIES = 3

# Proveedores con más de N vouchers (no-MEP) se saltan en la corrida masiva y se
# reportan aparte (entidades internas tipo 1EURO1/1ING01, inviables por UI).
MAX_VOUCHERS_PER_SUPPLIER = 500

# ── Base de datos ─────────────────────────────────────────────────────────────
# DB_CONNECTION=pgsql  →  PostgreSQL vía psycopg2
# DB_CONNECTION=sqlite (default)  →  SQLite local en outputs/tracker.db
DB_CONNECTION = os.getenv("DB_CONNECTION", "sqlite")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_DATABASE = os.getenv("DB_DATABASE", "euroturbot")
DB_USERNAME = os.getenv("DB_USERNAME", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
