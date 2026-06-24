from playwright.sync_api import Page, expect

from utils.logger import log

MODAL_TIMEOUT = 30000
# Espera máxima para que el modal Insert Invoice cierre tras SAVE (el spinner queda
# colgado, así que la señal real es el cierre del modal). CreateAPInvoice con muchas
# líneas tarda; con chunks de ~200 debería cerrar en pocos segundos.
SAVE_TIMEOUT_MS = 120000
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


class VoucherSearchTimeout(Exception):
    """El servidor TourplanNX expiró al buscar vouchers (proveedor con demasiados pendientes)."""
    pass


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
    # Filtrar por el modal Create Transaction (igual que create_bulk_transaction): sin
    # filtro, get_by_role("dialog") puede agarrar un dialog residual y matchear un OK
    # equivocado (ej. el botón tpinvoicelines disabled del Insert Invoice).
    dialog = page.get_by_role("dialog").filter(has_text="Create Transaction").last
    ok_btn = dialog.get_by_role("button", name="OK")
    expect(ok_btn).to_be_enabled(timeout=MODAL_TIMEOUT)
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

    # Señal de guardado = el modal Insert Invoice se cierra (dialogs disminuyen).
    # NO se espera el spinner: queda colgado (bug cosmético del UI) aunque el SAVE
    # haya completado. Polling hasta SAVE_TIMEOUT_MS.
    waited = 0
    step = 1000
    while waited < SAVE_TIMEOUT_MS:
        if page.get_by_role("dialog").count() < dialogs_before:
            log.info("    Invoice guardado (SAVE)")
            return
        page.wait_for_timeout(step)
        waited += step
    raise RuntimeError(
        "El modal de invoice sigue abierto tras SAVE (timeout) — posible cuelgue de CreateAPInvoice"
    )


def exit_invoice_line(page: Page) -> None:
    """Cierra solo la Invoice Line activa (EXIT) volviendo al Insert Invoice."""
    try:
        last = page.get_by_role("dialog").last
        last.get_by_role("button", name="EXIT").click(force=True)
        page.wait_for_timeout(600)
    except Exception:
        pass
    log.info("    Invoice Line cerrada (saltada)")


def _set_voucher_range(page: Page, voucher_from: str | None, voucher_to: str | None) -> None:
    """Setea VOUCHER FROM/TO en la pestaña SELECTION del modal Select Vouchers activo."""
    page.evaluate("""
        ([vFrom, vTo]) => {
            // El modal Select Vouchers es el último dialog abierto
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            const modal = dialogs[dialogs.length - 1];
            if (!modal) return;
            // Inputs editables numéricos del modal (VOUCHER FROM = [0], VOUCHER TO = [1])
            const inputs = Array.from(modal.querySelectorAll('input')).filter(
                i => !i.readOnly && !i.disabled
                  && Array.from(i.classList).some(c => c.includes('tpnumber'))
            );
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            const fire = (el, val) => {
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            };
            if (vFrom && inputs[0]) fire(inputs[0], vFrom);
            if (vTo   && inputs[1]) fire(inputs[1], vTo);
        }
    """, [voucher_from, voucher_to])
    log.info("    Rango vouchers: FROM=%s TO=%s", voucher_from, voucher_to)


_VOUCHER_ERROR_PATTERNS = [
    "GetVoucherDetails",
    "An error has occurred",
    "Error! 1026",
    "Execution Timeout",
    "1026",
]


def _check_voucher_search_error(page: Page) -> None:
    """Detecta un error/timeout del servidor tras SEARCH y lanza VoucherSearchTimeout.

    El error real del servidor es 'Error! 1026 ... -2 Execution Timeout Expired'
    (timeout de SQL Server cuando el rango VOUCHER FROM/TO es demasiado ancho).
    """
    error_text = page.evaluate("""
        (patterns) => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            for (const d of dialogs) {
                const t = d.textContent || '';
                if (patterns.some(p => t.includes(p))) {
                    return t.substring(0, 150).trim();
                }
            }
            return null;
        }
    """, _VOUCHER_ERROR_PATTERNS)
    if not error_text:
        return
    try:
        page.evaluate("""
            (patterns) => {
                const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
                for (const d of dialogs) {
                    const t = d.textContent || '';
                    if (patterns.some(p => t.includes(p))) {
                        const btn = d.querySelector('button');
                        if (btn) { btn.click(); return true; }
                    }
                }
                return false;
            }
        """, _VOUCHER_ERROR_PATTERNS)
    except Exception:
        pass
    raise VoucherSearchTimeout(f"TourplanNX timeout al buscar vouchers: {error_text}")


def _read_found_count(page: Page):
    """Lee el contador 'Found N' del modal Select Vouchers (None si no aparece)."""
    return page.evaluate("""
        () => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            const modal = dialogs[dialogs.length - 1];
            if (!modal) return null;
            const m = (modal.textContent || '').replace(/\\s+/g, ' ').match(/Found[\\s:]*([\\d,]+)/i);
            return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
        }
    """)


def _wait_for_search_results(page: Page, timeout_ms: int = 35000) -> int:
    """Espera el resultado del SEARCH sin depender del spinner (que se cuelga).

    La señal de fin es el contador 'Found' > 0. Chequea el error de timeout SQL
    en cada iteración. Devuelve el conteo encontrado (0 si no hubo resultados).
    """
    waited = 0
    step = 1000
    while waited < timeout_ms:
        _check_voucher_search_error(page)  # lanza VoucherSearchTimeout si hay Error! 1026
        found = _read_found_count(page)
        if found and found > 0:
            return found
        page.wait_for_timeout(step)
        waited += step
    return _read_found_count(page) or 0


def add_vouchers_via_search(
    page: Page,
    vouchers: list[str],
    voucher_from: str | None = None,
    voucher_to: str | None = None,
    select_all: bool = False,
) -> dict:
    """Carga múltiples vouchers usando el modal 'Select Vouchers' (lupa).

    Abre el modal desde la Invoice Line activa, aplica filtro VOUCHER FROM/TO,
    ejecuta SEARCH, selecciona y confirma con OK.

    Modos de selección:
      - select_all=False (default, proveedores chicos): SELECT ALL si los resultados
        coinciden con el Excel, o clicks individuales por voucher si hay extras.
      - select_all=True (modo chunked masivo): SELECT ALL siempre, cargando TODO lo
        pendiente del servidor en el rango. El grid virtualiza (no se puede contar por
        DOM), así que el conteo cargado se toma del contador 'Found'.

    Raises:
        VoucherSearchTimeout: si el servidor TourplanNX expira durante SEARCH.

    Returns:
        {"loaded": [...], "not_found": [...]}  (en modo select_all, "loaded" es un
        marcador con `found` elementos y "not_found" queda vacío).
    """
    target = {str(v).replace(",", "").strip() for v in vouchers if str(v).strip()}

    # 1. Click botón lupa ("Search for Voucher") desde la Invoice Line activa
    invoice_line = page.get_by_role("dialog").last
    lupa = invoice_line.get_by_role("button", name="Search for Voucher")
    lupa.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    lupa.click()
    log.info("    Select Vouchers: abriendo modal (lupa)...")

    # 2. Esperar el modal Select Vouchers
    sv = page.get_by_role("dialog").filter(has_text="Select Vouchers")
    sv.wait_for(state="visible", timeout=MODAL_TIMEOUT)

    # 3. Setear rango VOUCHER FROM/TO para acotar la búsqueda en el servidor
    if voucher_from or voucher_to:
        _set_voucher_range(page, voucher_from, voucher_to)

    # 4. Click SEARCH (button.tpsearch — evita ambigüedad con "Search for Supplier")
    sv.locator("button.tpsearch").click()
    log.info("    SEARCH ejecutado (FROM=%s TO=%s)...", voucher_from, voucher_to)

    # 5. Esperar el resultado por el contador 'Found' (NO el spinner, que se cuelga).
    #    _wait_for_search_results lanza VoucherSearchTimeout si hay Error! 1026.
    found = _wait_for_search_results(page)
    log.info("    SEARCH resultados: Found=%d", found)

    # ── Modo masivo: SELECT ALL siempre (carga todo lo pendiente del rango) ──
    if select_all:
        if found <= 0:
            log.warning("    SEARCH sin resultados (Found=0) — saliendo sin cargar")
            try:
                sv.get_by_role("button", name="EXIT").click(force=True)
                sv.wait_for(state="hidden", timeout=MODAL_TIMEOUT)
            except Exception:
                pass
            return {"loaded": [], "not_found": sorted(target)}
        sv.locator("button.tpselectall").click()
        # esperar a que OK se habilite (la selección server-side tarda)
        try:
            expect(sv.get_by_role("button", name="OK")).to_be_enabled(timeout=MODAL_TIMEOUT)
        except Exception:
            pass
        log.info("    SELECT ALL: %d vouchers (todo lo pendiente del rango)", found)
        sv.get_by_role("button", name="OK").click()
        log.info("    OK -> cargando %d lineas...", found)
        sv.wait_for(state="hidden", timeout=30000)
        page.wait_for_timeout(800)
        log.info("    Lupa completada (masivo): %d cargados", found)
        # 'loaded' es un marcador con `found` elementos (el grid virtualiza, no hay
        # números exactos); el matching por-voucher no aplica en este modo.
        return {"loaded": list(range(found)), "not_found": []}

    # ── Modo chico (comportamiento original): matching contra el Excel ──
    try:
        page.locator("tr.tpgrid td.tpcol-vouchernumber").first.wait_for(state="visible", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    rows_data: list[dict] = page.evaluate("""
        () => Array.from(document.querySelectorAll('tr.tpgrid'))
                  .map((row, idx) => {
                      const vc = row.querySelector('td.tpcol-vouchernumber');
                      return vc ? { index: idx, voucher: (vc.textContent || '').replace(/,/g, '').trim() } : null;
                  })
                  .filter(Boolean)
    """) or []

    result_vouchers = [r["voucher"] for r in rows_data]
    result_set = set(result_vouchers)

    if result_set and result_set == target:
        sv.locator("button.tpselectall").click()
        loaded = list(result_vouchers)
        log.info("    SELECT ALL: %d vouchers (resultados == Excel)", len(loaded))
    else:
        try:
            page.locator("tr.tpgrid td.tpcol-checkbox").first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        loaded = []
        for target_voucher in sorted(target):
            row_idx: int = page.evaluate("""
                (vnum) => {
                    const rows = Array.from(document.querySelectorAll('tr.tpgrid'));
                    for (let i = 0; i < rows.length; i++) {
                        const vc = rows[i].querySelector('td.tpcol-vouchernumber');
                        if (vc && vc.textContent.replace(/,/g, '').trim() === vnum) return i;
                    }
                    return -1;
                }
            """, str(target_voucher))
            if row_idx >= 0:
                page.locator("tr.tpgrid").nth(row_idx).locator("td.tpcol-checkbox").click(timeout=10000)
                _wait_for_spinner(page)
                loaded.append(target_voucher)
        log.info("    Clicks individuales: %d/%d seleccionados", len(loaded), len(result_vouchers))

    not_found = sorted(target - set(loaded))
    log.info("    Seleccionados %d/%d vouchers (%d no encontrados)",
             len(loaded), len(target), len(not_found))

    if not loaded:
        log.warning("    Ningún voucher encontrado en lupa — saliendo sin cargar")
        try:
            sv.get_by_role("button", name="EXIT").click(force=True)
            sv.wait_for(state="hidden", timeout=MODAL_TIMEOUT)
        except Exception:
            pass
        return {"loaded": [], "not_found": sorted(target)}

    sv.get_by_role("button", name="OK").click()
    log.info("    OK -> cargando %d lineas...", len(loaded))
    sv.wait_for(state="hidden", timeout=30000)
    _wait_for_spinner(page)
    page.wait_for_timeout(800)

    log.info("    Lupa completada: %d cargados, %d no encontrados", len(loaded), len(not_found))
    return {"loaded": loaded, "not_found": not_found}


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
    # Esperar que Angular desmonte todos los <dialog open> del DOM antes de continuar.
    # Sin esta espera, el tp-dialog en transición intercepta el próximo click INSERT.
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('dialog[open]').length === 0",
            timeout=8000,
        )
    except Exception:
        pass
    log.info("  abort_transaction: modales cerrados")
