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

# Umbral DURO: proveedores con MÁS de N registros se POSPONEN (quedan 'pending' y se
# loguean) en vez de procesarse, para que un proveedor monstruo (ej. 1ING01 con 52k
# registros) no bloquee al resto del archivo. Se procesan después en una corrida
# dedicada (subir/anular este umbral, o filtrar por --supplier). 0/None = sin límite.
MAX_VOUCHERS_DEFER_THRESHOLD = 2000

# Consultar GetAccountingTransactions para deduplicar facturas INV* ya creadas.
# DESACTIVADO: para proveedores con historial grande la respuesta es enorme y el
# JSON.parse bloquea el event loop del navegador (cuelga indefinidamente; el timeout
# de 90s no alcanza a dispararse porque el hilo JS está ocupado parseando). NO es
# necesario para la correctitud: el error 1038 'Reference Exists' ya se maneja por
# factura (se marca ok y se sigue). Reactivar solo si se acota la query del lado server.
READ_EXISTING_REFS = False

# Tamaño de cada factura (bloque de vouchers) en la carga chunked de proveedores
# grandes. El límite real lo impone el SAVE (CreateAPInvoice): 2108 líneas cuelga el
# servidor >7min. 200 es conservador; subir tras validar cuánto aguanta el SAVE.
VOUCHER_CHUNK_SIZE = 200

# Ancho máximo del rango VOUCHER FROM/TO de un chunk. El SEARCH hace timeout SQL
# cuando el rango numérico es muy ancho (no por cantidad): ancho ~300k OK, ~2M falla.
# 200k deja margen para zonas con vouchers dispersos.
VOUCHER_MAX_RANGE_WIDTH = 200_000

# Corte por hueco: si entre dos vouchers consecutivos (ordenados) hay un salto mayor a
# este valor, se corta un chunk nuevo. Mantiene los rangos DENSOS (menos vouchers ajenos
# devueltos por el SEARCH → escaneo del grid más rápido y menor riesgo de timeout SQL).
# No afecta la correctitud (la selección tilda solo los vouchers del Excel); es defensa
# en profundidad. 0 = desactivado.
VOUCHER_MAX_GAP = 50_000

# Filtro "Service Date To" del modal Select Vouchers (tab SELECTION). Acota la búsqueda
# a vouchers con fecha de servicio <= esta fecha, para NO cargar vouchers a futuro.
# Formato TourplanNX: 'DD/Mon/YYYY' (ej '31/Mar/2026'). None/"" = sin filtro de fecha.
# Actualizar por lote/período de pago.
SERVICE_DATE_TO = "31/Mar/2026"  # filtro "Service Date To" (2º input tpdate-servicedate)

# Tolerancia del REMAINDER al guardar un invoice. Si |INVOICE - ESPERADO| la supera,
# save_invoice es fail-closed: NO guarda. La tolerancia efectiva ESCALA con la cantidad de
# vouchers cargados para absorber el redondeo acumulado (Excel con decimales vs montos
# redondeados de TourplanNX): tol = max(base, por_voucher * n_cargados).
# Como la selección tilda SOLO vouchers nuestros por número (nunca ajenos), el descuadre
# restante es redondeo; un voucher con monto realmente distinto (> por_voucher) igual se
# detecta. Ajustar por_voucher si aparecen redondeos mayores.
INVOICE_REMAINDER_TOLERANCE = 0.01        # piso absoluto
INVOICE_TOLERANCE_PER_VOUCHER = 0.10      # margen de redondeo por voucher cargado

# ── Cheques (orden de pago) ─────────────────────────────────────────────────────
# Prefijo de la REFERENCE del cheque (análogo a "INV" del invoice): OP{row_index}{code}.
CHEQUE_REFERENCE_PREFIX = "OP"
# PAYMENT TYPE del cheque: EA1299 = "EA - CONTROL PAGOS" (mismo para ARS y USD).
CHEQUE_PAYMENT_TYPE = "EA1299"
# Proveedores exentos: sus Supplier_Code se saltean por completo (no se emite cheque).
CHEQUE_EXEMPT_FILE = BASE_DIR / "input" / "proveedores_exentos.csv"

# ── Base de datos ─────────────────────────────────────────────────────────────
# DB_CONNECTION=pgsql  →  PostgreSQL vía psycopg2
# DB_CONNECTION=sqlite (default)  →  SQLite local en outputs/tracker.db
DB_CONNECTION = os.getenv("DB_CONNECTION", "sqlite")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_DATABASE = os.getenv("DB_DATABASE", "euroturbot")
DB_USERNAME = os.getenv("DB_USERNAME", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# ── API del monitor (consumo desde páginas de terceros) ─────────────────────────
# MONITOR_API_KEY      → clave de LECTURA que exigen los endpoints GET a requests
#                        cross-origin (terceros). El dashboard same-origin no la pide.
# MONITOR_ADMIN_KEY    → clave opcional para operar los POST de control desde fuera
#                        (server-to-server). Vacía = control solo same-origin.
# MONITOR_CORS_ORIGINS → lista separada por comas de dominios autorizados a consumir
#                        la API por CORS. Vacía = ningún origen cross permitido.
MONITOR_API_KEY = os.getenv("MONITOR_API_KEY", "")
MONITOR_ADMIN_KEY = os.getenv("MONITOR_ADMIN_KEY", "")
MONITOR_CORS_ORIGINS = [
    o.strip() for o in os.getenv("MONITOR_CORS_ORIGINS", "").split(",") if o.strip()
]
