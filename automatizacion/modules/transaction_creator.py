from playwright.sync_api import Page, expect

from utils.logger import log

MODAL_TIMEOUT = 10000


def _wait_for_spinner(page: Page) -> None:
    try:
        page.locator(".spinner").wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    page.locator(".spinner").wait_for(state="hidden", timeout=15000)


def fill_currency(page: Page, currency_code: str) -> None:
    page.evaluate("""
        (code) => {
            const inputs = document.querySelectorAll('input');
            for (const inp of inputs) {
                if (inp.value.includes('ARS') || inp.value.includes('Pesos')) {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(inp, code);
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            }
            return false;
        }
    """, currency_code)
    page.wait_for_timeout(2000)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(500)
    try:
        row = page.locator(".dropdown table tr").filter(has_text=currency_code).first
        row.wait_for(state="visible", timeout=4000)
        row.click()
    except Exception:
        page.evaluate("""
            (code) => {
                const dropdowns = document.querySelectorAll('.dropdown, .tpdropdown');
                for (const dd of dropdowns) {
                    if (dd.offsetParent !== null) {
                        const row = dd.querySelector('tr');
                        if (row && row.textContent.includes(code)) {
                            row.click();
                            return true;
                        }
                    }
                }
                return false;
            }
        """, currency_code)
        page.wait_for_timeout(500)


def create_transaction(page: Page, row_data: dict, row_index: int) -> None:
    log.info("  Llenando formulario Create Transaction...")

    _wait_for_spinner(page)

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Create Transaction")).to_be_visible(timeout=MODAL_TIMEOUT)

    ref = f"{row_index}_{row_data['Supplier_Code'].strip()}_{row_data['Voucher_Number']}"
    dialog.locator("input.tpdescription-transactionreference").fill(ref)

    voucher = str(row_data["Voucher_Number"])
    dialog.locator("input.tpnumber-vouchernumber").fill(voucher)

    currency = row_data["Service_Cost_Currency"].strip()
    fill_currency(page, currency)

    log.info("  Formulario llenado correctamente (OK sin presionar)")
