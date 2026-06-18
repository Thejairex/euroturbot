from playwright.sync_api import Page, expect

from config.urls import spa_url
from utils.logger import log


SEARCH_TIMEOUT = 15000


def open_supplier(page: Page, supplier_code: str) -> None:
    code = supplier_code.strip()
    log.info("  Buscando proveedor: %s", code)

    input_el = page.locator("#searchSupplier input[type='text']").first
    # Si el input no es editable (proveedor anterior no salió limpio), navegar a creditor
    try:
        if not input_el.is_editable(timeout=2000):
            log.warning("  Input de búsqueda no editable — navegando a creditor para limpiar estado")
            page.goto(spa_url("creditor"))
            page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    expect(input_el).to_be_visible(timeout=SEARCH_TIMEOUT)
    input_el.fill(code)

    page.wait_for_selector(".dropdown table", timeout=SEARCH_TIMEOUT)
    row = page.locator(f".dropdown table tr").filter(has_text=code).first
    expect(row).to_be_visible(timeout=SEARCH_TIMEOUT)
    row.click()

    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("button", name="Save")).to_be_visible(timeout=SEARCH_TIMEOUT)
    page.wait_for_timeout(1500)
    log.info("  Proveedor abierto correctamente")
