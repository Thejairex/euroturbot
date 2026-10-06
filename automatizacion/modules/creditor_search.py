import re

from playwright.sync_api import Page, expect

from config.urls import spa_url
from core.exceptions import SupplierNotFoundError
from modules.supplier_nav import exit_supplier
from utils.logger import log


SEARCH_TIMEOUT = 30000


def open_supplier(page: Page, supplier_code: str) -> bool:
    """Abre el proveedor en Creditors. Devuelve True si el dropdown lo mostró marcado
    'DELETED' (proveedor eliminado en TourplanNX — el registro completo, incluido
    Transactions/INSERT, queda de solo lectura para siempre)."""
    code = supplier_code.strip()
    log.info("  Buscando proveedor: %s", code)

    input_el = page.locator("#searchSupplier input[type='text']").first
    # Si el input no es editable (proveedor anterior no cerró), recuperar con reload
    # completo: un goto por hash NO reinicia Angular si ya está en #/creditor con un
    # proveedor abierto. page.reload() fuerza re-init y vuelve al search limpio.
    try:
        if not input_el.is_editable(timeout=2000):
            log.warning("  Input de búsqueda no editable — recargando página para limpiar estado")
            page.goto(spa_url("creditor"))
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)
    except Exception:
        pass

    expect(input_el).to_be_editable(timeout=SEARCH_TIMEOUT)
    input_el.click()        # foco explícito para que Angular registre el campo
    input_el.fill(code)
    page.wait_for_timeout(200)  # debounce Angular antes de esperar el dropdown

    # Primer intento rápido; si Angular no disparó el evento de búsqueda, re-trigger
    # con la tecla End (activa keyup sin modificar el texto) y espera larga.
    try:
        page.wait_for_selector(".dropdown table", timeout=5000)
    except Exception:
        log.warning("  Dropdown no apareció — re-trigger Angular (End)...")
        input_el.press("End")
        page.wait_for_selector(".dropdown table", timeout=SEARCH_TIMEOUT)

    row = page.locator(".dropdown table tr").filter(has_text=code).first
    if not row.is_visible(timeout=3000):
        raise SupplierNotFoundError(f"Proveedor '{code}' no encontrado en TourplanNX")
    row_text = row.inner_text()
    is_deleted = "deleted" in row_text.lower()

    # Reintenta el click de la fila (no solo la espera) si el botón Save nunca aparece:
    # confirmado en vivo (2026-10-01, 1USH06, 358 filas fallidas) que row.click() a veces
    # no registra — la página se queda en la pantalla de búsqueda, nunca transiciona al
    # creditor — mismo patrón de click interceptado por un overlay/spinner ya visto y
    # resuelto en otros lados de este proyecto (ver _open_transaction_filters). Se usa
    # click nativo por JS (bypassea hit-testing) en vez de Playwright normal, y se
    # reintenta hasta 3 veces antes de rendirse.
    save_btn = page.get_by_role("button", name="Save")
    for attempt in range(3):
        row.evaluate("el => el.click()")
        page.wait_for_load_state("networkidle")
        try:
            expect(save_btn).to_be_visible(timeout=SEARCH_TIMEOUT)
            break
        except Exception:
            if attempt == 2:
                raise
            log.warning("  Proveedor %s: 'Save' no apareció tras el click (intento %d/3) — "
                        "reintentando", code, attempt + 1)
            # Releer la fila: si la búsqueda se resetió sola, el dropdown puede haber
            # cambiado de posición/instancia.
            row = page.locator(".dropdown table tr").filter(has_text=code).first

    page.wait_for_timeout(1500)
    log.info("  Proveedor abierto correctamente%s", " (DELETED)" if is_deleted else "")
    return is_deleted


def insert_disabled(page: Page) -> bool:
    """Confirma en el DOM si el botón INSERT de Transactions está deshabilitado. Un
    proveedor DELETED en TourplanNX deja el registro entero de solo lectura para siempre
    (INSERT nunca se habilita) — ver open_supplier. Compartida entre el pipeline de
    invoices (core/pipeline.py) y el de cheques (checks/cheque_pipeline.py)."""
    try:
        return page.locator("#creditorview").get_by_role("button", name="INSERT").is_disabled(timeout=3000)
    except Exception:
        return False


def reactivate_creditor(page: Page, supplier_code: str) -> bool:
    """Sale y reabre el proveedor (queda en tab DETAILS), va a ANALYSIS y desmarca
    'Flag Creditor as Deleted' (#SupplierIsDeleted) en Miscellaneous, luego guarda.

    Confirmado en vivo: un creditor con SupplierIsDeleted=true queda con casi todo el
    formulario readonly e INSERT de Transactions deshabilitado, pero el botón SAVE del
    creditor sigue funcional — se puede desmarcar el flag y persistirlo.

    IMPORTANTE: esta función SOLO desmarca (uncheck). Ningún flujo del pipeline debe
    llamar a checkbox.check() sobre este campo — una vez reactivado un proveedor no se
    lo vuelve a marcar DELETED automáticamente.

    Devuelve True si el checkbox estaba marcado y se guardó el cambio; False si ya
    estaba desmarcado (el INSERT deshabilitado no se debía a este flag).
    """
    exit_supplier(page)
    open_supplier(page, supplier_code)

    # Match case-insensitivo: el texto accesible de la tab puede venir en minúscula/
    # mixed-case con text-transform:uppercase por CSS (el mismo motivo por el que
    # get_by_role(name=...) en el resto del código NUNCA usa exact=True).
    analysis_tab = page.get_by_text(re.compile(r"^\s*analysis\s*$", re.IGNORECASE)).first
    expect(analysis_tab).to_be_visible(timeout=SEARCH_TIMEOUT)
    analysis_tab.click(force=True)
    page.wait_for_timeout(500)

    checkbox = page.locator("#SupplierIsDeleted")
    if not checkbox.is_checked():
        return False

    # Un creditor SupplierIsDeleted=true deja casi todo el formulario "readonly" por
    # estilo (aunque el input en sí no tenga el atributo disabled), lo que bloquea la
    # comprobación de "actionability" de Playwright (visible/estable/recibe eventos) y
    # cuelga uncheck()/click() normales por 30s. Se dispara un click nativo del DOM
    # (bypassea overlays/pointer-events, pero sigue emitiendo click/change reales para
    # que Angular lo detecte — mismo espíritu que el fix ya documentado para CURRENCY).
    checkbox.evaluate("el => el.click()")
    page.wait_for_timeout(300)
    if checkbox.is_checked():
        log.warning("  El click nativo no logró desmarcar #SupplierIsDeleted")
        return False

    save_btn = page.locator("#creditorview").get_by_role("button", name="SAVE")
    expect(save_btn).to_be_enabled(timeout=SEARCH_TIMEOUT)
    save_btn.click()
    page.wait_for_timeout(1500)

    # El ida-y-vuelta de exit/reopen/tab-switch dentro de la MISMA sesión de Angular deja
    # algo corrupto (probablemente un <dialog> del pool interno, o un componente que no
    # se destruyó bien) que rompe el modal Select Vouchers para el resto de esa sesión —
    # confirmado en vivo: la reactivación en TourplanNX queda bien guardada, pero los
    # chunks posteriores del mismo proveedor fallan igual. Un reload completo reinicia
    # Angular de cero (mismo recurso que ya usa open_supplier para el caso "input no
    # editable"), y recién ahí se reabre el proveedor para seguir con la carga normal.
    log.info("  Reactivación guardada — recargando página para limpiar el estado de Angular...")
    page.goto(spa_url("creditor"))
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    open_supplier(page, supplier_code)
    return True
