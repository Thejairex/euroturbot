from playwright.sync_api import Page, expect

from config.urls import spa_url
from core.exceptions import SupplierNotFoundError
from utils.logger import log


SEARCH_TIMEOUT = 30000


def open_supplier(page: Page, supplier_code: str) -> None:
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
    row.click()

    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("button", name="Save")).to_be_visible(timeout=SEARCH_TIMEOUT)
    page.wait_for_timeout(1500)
    log.info("  Proveedor abierto correctamente")
