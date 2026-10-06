from playwright.sync_api import Page, expect

from utils.logger import log

NAV_TIMEOUT = 10000


def click_hamburger(page: Page) -> None:
    expect(page.locator(".hamburger")).to_be_visible(timeout=NAV_TIMEOUT)
    page.evaluate("document.querySelector('.hamburger').click()")


def _wait_for_loading_data(page: Page, timeout_ms: int = 30000) -> None:
    """Espera a que desaparezca el dialog "Loading data..." si está presente.

    Proveedores con historial enorme (ej. 1EURO1, la cuenta de la propia empresa) pueden
    tardar más que NAV_TIMEOUT en terminar de cargar tras abrirse — confirmado en vivo
    (2026-09-30): navigate_to_transactions fallaba buscando "TRANSACTIONS" en el nav
    porque la página todavía mostraba "Loading data..." encima de todo. No rompe si
    nunca apareció (get_by_text con count()==0 es instantáneo)."""
    try:
        loading = page.get_by_text("Loading data...")
        if loading.count() > 0:
            loading.first.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass


def navigate_to_transactions(page: Page) -> None:
    log.info("  Abriendo sidebar y navegando a Transactions...")
    _wait_for_loading_data(page)
    click_hamburger(page)

    nav = page.locator("nav")
    expect(nav.get_by_text("ACCOUNTING")).to_be_visible(timeout=NAV_TIMEOUT)
    nav.get_by_text("ACCOUNTING").click(force=True)
    page.wait_for_timeout(500)

    # "Loading data..." puede reaparecer recién ACÁ (ej. al expandir ACCOUNTING dispara
    # una carga propia de esa sección) — confirmado en vivo (2026-09-30, 1EURO1): el
    # chequeo de arriba no alcanzaba porque en ese momento todavía no había aparecido.
    _wait_for_loading_data(page)
    expect(nav.get_by_text("TRANSACTIONS")).to_be_visible(timeout=NAV_TIMEOUT)
    nav.get_by_text("TRANSACTIONS").first.click(force=True)
    page.wait_for_timeout(500)


def exit_supplier(page: Page, attempts: int = 3) -> None:
    log.info("  Saliendo del proveedor...")
    # force=True: un <dialog> residual del pool de Angular (ej. tras guardar un cheque)
    # puede quedar interceptando pointer events sobre este botón aunque esté visible y
    # habilitado (confirmado en vivo: "dialog ... intercepts pointer events"), mismo
    # patrón ya manejado para ACCOUNTING/TRANSACTIONS.
    #
    # Reintenta el click de EXIT (no solo la espera): tras guardar un cheque gigante
    # (930+ invoices) la página puede tardar más de NAV_TIMEOUT en asentarse, o el click
    # puede no haber registrado — confirmado en vivo (2026-09-30, 1ATRA1 con 930
    # invoices): quedaba "readOnly" tras un solo intento de 10s.
    input_el = page.locator("#searchSupplier input[type='text']").first
    for attempt in range(attempts):
        page.get_by_role("button", name="EXIT").first.click(force=True)
        try:
            # Validar editable (no solo visible): el input readonly también es visible,
            # así que to_be_visible daría falso éxito aunque el proveedor siga abierto.
            expect(input_el).to_be_editable(timeout=NAV_TIMEOUT)
            return
        except Exception:
            if attempt == attempts - 1:
                raise
            log.warning("  EXIT no registró (intento %d/%d) — reintentando",
                        attempt + 1, attempts)
