from playwright.sync_api import Page, expect

from utils.logger import log

MODAL_TIMEOUT = 10000
EXPECTED_TOTAL_SELECTOR = "input.tpnumber-invoices"
VOUCHER_LINE_OK = "button.tpok.tpprimarysystembutton"
INVOICE_INSERT = "button.tpinsert"
INVOICE_SAVE = "button.tpsave"
ACCOUNT_SELECTOR = "input.tpcode.tpcode6.tpuppercase"
VOUCHER_INPUT = "input.tpnumber-vouchernumber:not(.tpreadonly)"


class InvalidAccountError(Exception):
    """La cuenta contable autocompletada quedó inválida (clase tpinvalid)."""
    def __init__(self, message: str, voucher: str | None = None, account: str | None = None):
        super().__init__(message)
        self.voucher = voucher
        self.account = account


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

    supplier_code = (row_data.get("Supplier_Code") or "").strip()
    reference = f"INV{row_index}{supplier_code}"
    dialog.locator("input.tpdescription-transactionreference").fill(reference)

    voucher = str(row_data["Voucher_Number"])
    dialog.locator("input.tpnumber-vouchernumber").fill(voucher)

    currency = row_data["Service_Cost_Currency"].strip()
    fill_currency(page, currency)

    log.info("  Formulario llenado correctamente (OK sin presionar)")


def create_bulk_transaction(page: Page, expected_total: float, currency: str, reference: str) -> None:
    """Llena el modal Create Transaction para el proceso masivo.

    Rellena REFERENCE + EXPECTED TOTAL + CURRENCY. No toca VOUCHER NO.
    No hace click en OK ni en ningún botón de confirmación.
    """
    log.info("  Llenando formulario masivo (total=%.2f, currency=%s, ref=%s)...", expected_total, currency, reference)

    _wait_for_spinner(page)

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Create Transaction")).to_be_visible(timeout=MODAL_TIMEOUT)

    dialog.locator("input.tpdescription-transactionreference").fill(reference)

    dialog.locator(EXPECTED_TOTAL_SELECTOR).fill(f"{expected_total:.2f}")

    fill_currency(page, currency)

    log.info("  Formulario masivo llenado (EXPECTED TOTAL=%.2f, currency=%s, OK sin presionar)", expected_total, currency)


def confirm_bulk_transaction(page: Page) -> None:
    """Confirma el Create Transaction haciendo click en OK.

    Espera a que aparezca el modal Insert Invoice (con botón INSERT).
    Luego abre la primera Invoice Line haciendo click en INSERT.
    """
    dialog = page.get_by_role("dialog")
    ok_btn = dialog.get_by_role("button", name="OK")
    ok_btn.click()
    page.locator(VOUCHER_INPUT).wait_for(state="visible", timeout=MODAL_TIMEOUT)
    log.info("  Create Transaction confirmado — Insert Invoice + Invoice Line abiertos")


def add_voucher_line(page: Page, voucher_number: str, is_first: bool) -> None:
    """Carga un voucher en Invoice Line y confirma la línea.

    Si no es el primer voucher, primero hace click en INSERT del Insert Invoice
    para abrir una nueva Invoice Line.

    Raises:
        InvalidAccountError: si la cuenta contable quedó inválida tras cargar el voucher.
    """
    if not is_first:
        # Desde Insert Invoice (solo 1 dialog abierto), click INSERT para abrir nueva Invoice Line
        dialog = page.get_by_role("dialog")
        dialog.locator(INVOICE_INSERT).wait_for(state="visible", timeout=MODAL_TIMEOUT)
        dialog.locator(INVOICE_INSERT).click(force=True)
        # Esperar el input editable antes de continuar (igual que confirm_bulk_transaction)
        page.locator(VOUCHER_INPUT).wait_for(state="visible", timeout=MODAL_TIMEOUT)

    # El input del voucher está en la última Invoice Line abierta (el último dialog)
    invoice_line = page.get_by_role("dialog").last
    voucher_input = invoice_line.locator(VOUCHER_INPUT)
    voucher_input.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    voucher_input.fill(str(voucher_number))
    voucher_input.press("Tab")

    # Esperar que Angular procese (spinner + Angular binding)
    _wait_for_spinner(page)
    page.wait_for_timeout(1500)

    # Verificar si la Invoice Line se cerró sola (auto-close al account ser válido)
    # o si sigue abierta esperando confirmación
    dialogs_after = page.get_by_role("dialog").count()
    if dialogs_after > 1:
        # Invoice Line sigue abierta — chequear cuenta DEBIT
        try:
            debit_account = page.get_by_role("dialog").last.locator(ACCOUNT_SELECTOR).first
            classes = debit_account.get_attribute("class") or ""
            if "tpinvalid" in classes:
                try:
                    acct_val = debit_account.input_value()
                except Exception:
                    acct_val = "?"
                raise InvalidAccountError(
                    f"Cuenta DEBIT inválida para voucher {voucher_number} (cuenta: {acct_val})",
                    voucher=str(voucher_number), account=acct_val,
                )
        except InvalidAccountError:
            raise
        except Exception:
            pass

        # Click OK en el modal Invoice Line para confirmar la línea
        invoice_line_ok = page.get_by_role("dialog").last.get_by_role("button", name="OK")
        try:
            expect(invoice_line_ok).to_be_enabled(timeout=8000)
        except Exception:
            raise InvalidAccountError(
                f"OK deshabilitado para voucher {voucher_number} — cuenta contable posiblemente inválida",
                voucher=str(voucher_number),
            )
        invoice_line_ok.click()
        page.wait_for_timeout(500)

    _wait_for_spinner(page)

    _wait_for_spinner(page)
    log.info("    Voucher %s cargado OK", voucher_number)


def read_invoice_totals(page: Page) -> dict:
    """Lee INVOICE TOTAL, EXPECTED TOTAL y REMAINDER del Insert Invoice.

    Los 4 totales comparten la clase tpnumber-invoices tpreadonly en este orden:
    [0]=INVOICE TOTAL, [1]=TAX AMOUNT, [2]=EXPECTED TOTAL, [3]=REMAINDER

    Returns:
        {"invoice_total": float, "expected_total": float, "remainder": float}
    """
    parsed = page.evaluate("""
        () => {
            const inputs = document.querySelectorAll(
                'dialog[open] input.tpnumber-invoices.tpreadonly'
            );
            const clean = v => parseFloat((v || '0').replace(/,/g, '')) || 0;
            return {
                invoice_total:  clean(inputs[0] ? inputs[0].value : null),
                expected_total: clean(inputs[2] ? inputs[2].value : null),
                remainder:      clean(inputs[3] ? inputs[3].value : null),
            };
        }
    """)
    log.info("  Totales Insert Invoice: INVOICE=%.2f EXPECTED=%.2f REMAINDER=%.2f",
             parsed.get("invoice_total", 0),
             parsed.get("expected_total", 0),
             parsed.get("remainder", 0))
    return parsed


def save_invoice(page: Page) -> None:
    """Guarda el invoice (Insert Invoice) clickeando SAVE.

    Funciona aunque el REMAINDER no sea 0 (TourplanNX lo permite). Cuando el
    REMAINDER != 0, TourplanNX abre un diálogo "Warning, Invoice total mismatch"
    con botones NO/YES; se confirma con YES.
    """
    dialogs_before = page.get_by_role("dialog").count()
    dialog = page.get_by_role("dialog").last
    save_btn = dialog.locator(INVOICE_SAVE)
    save_btn.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    save_btn.click(force=True)
    page.wait_for_timeout(1000)

    # Si el invoice no cuadra, aparece el diálogo de confirmación. Click YES.
    try:
        warning = page.get_by_role("dialog").filter(has_text="Invoice total mismatch")
        if warning.count() > 0:
            warning.last.get_by_role("button", name="YES").click()
            log.info("    Warning de descuadre confirmado (YES)")
            page.wait_for_timeout(800)
    except Exception:
        pass

    _wait_for_spinner(page)
    page.wait_for_timeout(1000)

    # Verificar que el modal Insert Invoice se cerró (guardado efectivo). Si sigue
    # abierto, algún diálogo no se manejó — abortar para no colgar el exit_supplier.
    dialogs_after = page.get_by_role("dialog").count()
    if dialogs_after >= dialogs_before:
        raise RuntimeError(
            "El modal de invoice sigue abierto tras SAVE — posible diálogo no manejado"
        )
    log.info("    Invoice guardado (SAVE)")


def exit_invoice_line(page: Page) -> None:
    """Cierra solo la Invoice Line activa (EXIT) volviendo al Insert Invoice."""
    try:
        last = page.get_by_role("dialog").last
        last.get_by_role("button", name="EXIT").click(force=True)
        page.wait_for_timeout(600)
    except Exception:
        pass
    log.info("    Invoice Line cerrada (saltada)")


def abort_transaction(page: Page) -> None:
    """Sale de los modales de voucher en cascada hasta volver a la lista de Transactions.

    Usa force=True para sortear las intercepciones de Angular entre modales anidados.
    """
    for _ in range(5):
        try:
            dialogs = page.get_by_role("dialog")
            count = dialogs.count()
            if count == 0:
                break
            # Cerrar desde el más interno (último) hacia afuera
            last = dialogs.nth(count - 1)
            exit_btn = last.get_by_role("button", name="EXIT")
            exit_btn.click(force=True)
            page.wait_for_timeout(800)
        except Exception:
            break
    log.info("  abort_transaction: modales cerrados")
