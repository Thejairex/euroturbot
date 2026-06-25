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

# Umbral a partir del cual un proveedor se carga en modo "chunked": en vez de una
# sola factura, se divide en varias facturas de VOUCHER_CHUNK_SIZE vouchers c/u.
# (Antes este umbral saltaba al proveedor; ahora habilita la carga por chunks.)
MAX_VOUCHERS_PER_SUPPLIER = 500

# Tamaño de cada factura (bloque de vouchers) en la carga chunked de proveedores
# grandes. El límite real lo impone el SAVE (CreateAPInvoice): 2108 líneas cuelga el
# servidor >7min. 200 es conservador; subir tras validar cuánto aguanta el SAVE.
VOUCHER_CHUNK_SIZE = 200

# Ancho máximo del rango VOUCHER FROM/TO de un chunk. El SEARCH hace timeout SQL
# cuando el rango numérico es muy ancho (no por cantidad): ancho ~300k OK, ~2M falla.
# 200k deja margen para zonas con vouchers dispersos.
VOUCHER_MAX_RANGE_WIDTH = 200_000

# ── Cheques (orden de pago) ─────────────────────────────────────────────────────
# Prefijo de la REFERENCE del cheque (análogo a "INV" del invoice): OP{row_index}{code}.
CHEQUE_REFERENCE_PREFIX = "OP"
# PAYMENT TYPE del cheque: EA1299 = "EA - CONTROL PAGOS" (mismo para ARS y USD).
CHEQUE_PAYMENT_TYPE = "EA1299"

# ── Base de datos ─────────────────────────────────────────────────────────────
# DB_CONNECTION=pgsql  →  PostgreSQL vía psycopg2
# DB_CONNECTION=sqlite (default)  →  SQLite local en outputs/tracker.db
DB_CONNECTION = os.getenv("DB_CONNECTION", "sqlite")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_DATABASE = os.getenv("DB_DATABASE", "euroturbot")
DB_USERNAME = os.getenv("DB_USERNAME", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
