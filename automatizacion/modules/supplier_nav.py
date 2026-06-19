from playwright.sync_api import Page, expect

from utils.logger import log

NAV_TIMEOUT = 10000


def click_hamburger(page: Page) -> None:
    expect(page.locator(".hamburger")).to_be_visible(timeout=NAV_TIMEOUT)
    page.evaluate("document.querySelector('.hamburger').click()")


def navigate_to_transactions(page: Page) -> None:
    log.info("  Abriendo sidebar y navegando a Transactions...")
    click_hamburger(page)

    nav = page.locator("nav")
    expect(nav.get_by_text("ACCOUNTING")).to_be_visible(timeout=NAV_TIMEOUT)
    nav.get_by_text("ACCOUNTING").click(force=True)
    page.wait_for_timeout(500)

    expect(nav.get_by_text("TRANSACTIONS")).to_be_visible(timeout=NAV_TIMEOUT)
    nav.get_by_text("TRANSACTIONS").first.click(force=True)
    page.wait_for_timeout(500)


def exit_supplier(page: Page) -> None:
    log.info("  Saliendo del proveedor...")
    page.get_by_role("button", name="EXIT").first.click()
    # Validar editable (no solo visible): el input readonly también es visible, así que
    # to_be_visible daría falso éxito aunque el proveedor siga abierto.
    expect(page.locator("#searchSupplier input[type='text']").first).to_be_editable(timeout=NAV_TIMEOUT)
