from playwright.sync_api import Page, expect

from config.settings import (
    SERVICE_DATE_TO,
    INVOICE_REMAINDER_TOLERANCE,
    INVOICE_TOLERANCE_PER_VOUCHER,
)
from utils.logger import log


def invoice_tolerance(n_vouchers: int) -> float:
    """Tolerancia efectiva del REMAINDER: escala con la cantidad de vouchers cargados para
    absorber el redondeo acumulado. tol = max(piso, por_voucher * n)."""
    return max(INVOICE_REMAINDER_TOLERANCE, INVOICE_TOLERANCE_PER_VOUCHER * max(1, n_vouchers))

MODAL_TIMEOUT = 30000
# Espera máxima para que el botón lupa ("Search for Voucher") aparezca después de OK.
# El servidor puede tardar > 30s en abrir Insert Invoice para proveedores grandes.
# Si supera este límite se lanza VoucherSearchTimeout → activa subdivisión del chunk.
LUPA_VISIBLE_TIMEOUT = 90000
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


class ReferenceExistsError(Exception):
    """TourplanNX rechazó la transacción con error 1038: la referencia ya existe."""
    pass


class InvoiceSaveError(Exception):
    """TourplanNX rechazó el SAVE del invoice con un diálogo de error (ej. 'Error! 1006 …
    No currency conversion found from CLP to ARS …'). NO es un cuelgue: es un error de
    datos/config del servidor (falta tipo de cambio, cuenta, etc.). Se marca el chunk failed
    con el mensaje real en vez de esperar el timeout completo."""
    pass


class InvoiceMismatchError(Exception):
    """El total del invoice no coincide con el esperado (REMAINDER != 0).

    Se lanza ANTES de guardar (fail-closed): en vez de aceptar el warning "Invoice total
    mismatch" y guardar una factura con vouchers de más/de menos, se aborta la transacción
    y se marca el chunk como failed para revisión. Previene el bug de sobre-selección en
    modo masivo (SELECT ALL barría vouchers ajenos dentro del rango numérico).
    """
    def __init__(self, message: str, invoice_total: float | None = None,
                 expected_total: float | None = None, remainder: float | None = None):
        super().__init__(message)
        self.invoice_total = invoice_total
        self.expected_total = expected_total
        self.remainder = remainder


def _wait_for_spinner(page: Page) -> None:
    try:
        page.locator(".spinner").wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    try:
        page.locator(".spinner").wait_for(state="hidden", timeout=60000)
    except Exception:
        # El spinner de TourplanNX a veces queda colgado indefinidamente aunque el
        # servidor terminó de procesar. Continuar igual — el form puede estar listo.
        log.warning("  Spinner sigue visible tras 60s — continuando")


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
    # No esperar aquí: el wait real es lupa.wait_for() dentro de add_vouchers_via_search.
    # El wait original (VOUCHER_INPUT) matcheaba el Create Transaction form mismo —
    # era instantáneo pero falso. "Search for Voucher" no aparece a tiempo desde acá.
    page.wait_for_timeout(800)
    # Detectar error de TourplanNX tras el OK (ej. 1038 Reference Exists).
    # - get_by_role("dialog"): solo dialogs abiertos en el a11y tree (descarta pool cerrado).
    # - text_content(): lee todo el DOM, inner_text() puede devolver vacío si Angular
    #   aún no terminó de renderizar el contenido visible.
    # - Requiere "Transaction error" en el texto: evita falsos positivos de dialogs
    #   del pool que tienen "Error!" en el DOM sin contenido de error real.
    error_dialog = page.get_by_role("dialog").filter(has_text="Error!")
    if error_dialog.count() > 0:
        page.wait_for_timeout(400)  # dejar que Angular renderice el contenido
        try:
            error_text = (error_dialog.last.text_content() or "").strip()
        except Exception:
            error_text = ""
        is_real_error = error_text and ("Transaction error" in error_text or "Reference Exists" in error_text)
        if is_real_error:
            log.warning("  Error TourplanNX tras OK: %s", error_text)
            try:
                error_dialog.last.get_by_role("button").last.click(force=True)
                page.wait_for_timeout(300)
            except Exception:
                pass
            if "Reference Exists" in error_text:
                raise ReferenceExistsError(f"1038 Reference Exists: {error_text}")
            raise Exception(f"TourplanNX error tras OK: {error_text}")
        else:
            log.debug("  Dialog 'Error!' sin código reconocible (falso positivo) — ignorando: %r", error_text[:80] if error_text else "")
    log.info("  Create Transaction confirmado (OK clickeado)")


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
        # Igual que confirm_bulk_transaction: usar lupa en lugar de VOUCHER_INPUT
        # para evitar strict mode en el pool de tp-dialog de Angular.
        page.get_by_role("button", name="Search for Voucher").wait_for(state="visible", timeout=MODAL_TIMEOUT)

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


def save_invoice(page: Page, tolerance: float = INVOICE_REMAINDER_TOLERANCE,
                 expected_override: float | None = None) -> None:
    """Guarda el invoice (Insert Invoice) clickeando SAVE — fail-closed ante descuadre.

    FAIL-CLOSED: si el total cargado no coincide con lo esperado (|diferencia| > tolerance)
    NO se guarda. Previene el bug de sobre-selección (cargar vouchers ajenos).

    `expected_override`: cuando algunos vouchers del chunk NO estaban en la lupa y se
    cargaron solo los hallados, el EXPECTED del formulario (que se tipeó con el total del
    chunk COMPLETO) ya no aplica. Se pasa acá la suma del costo de los vouchers REALMENTE
    cargados; la validación compara INVOICE contra ese valor. Como solo tildamos vouchers
    NUESTROS (nunca ajenos), que INVOICE == suma(costo de los cargados) garantiza que la
    factura contiene exactamente los vouchers correctos con sus montos correctos. En ese
    caso, el warning "Invoice total mismatch" (por los vouchers salteados) se acepta con YES.

    Comportamiento:
      1. Se calcula la diferencia efectiva (INVOICE − esperado); si excede la tolerancia →
         InvoiceMismatchError (sin clickear SAVE). Frena descuadres reales.
      2. Dentro de tolerancia: SAVE, y si aparece el warning se confirma con YES.

    Raises:
        InvoiceMismatchError: el invoice excede la tolerancia; NO se guardó nada.
    """
    # 1. Guarda primaria: verificar que el total cuadre ANTES de intentar guardar.
    totals = read_invoice_totals(page)
    invoice_total = totals.get("invoice_total", 0.0)
    esperado = expected_override if expected_override is not None else totals.get("expected_total", 0.0)
    remainder = invoice_total - esperado
    if abs(remainder) > tolerance:
        raise InvoiceMismatchError(
            f"Invoice descuadrado (INVOICE={invoice_total:.2f} "
            f"ESPERADO={esperado:.2f} DIF={remainder:.2f}) — no se guarda (fail-closed)",
            invoice_total=invoice_total,
            expected_total=esperado,
            remainder=remainder,
        )

    dialogs_before = page.get_by_role("dialog").count()
    dialog = page.get_by_role("dialog").last
    save_btn = dialog.locator(INVOICE_SAVE)
    save_btn.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    save_btn.click(force=True)
    page.wait_for_timeout(1000)

    # 2. Dentro de tolerancia (redondeo): si TourplanNX abre el warning de descuadre,
    #    confirmar con YES para aceptar la diferencia mínima y guardar. Seguro: los
    #    descuadres grandes ya se rechazaron en el paso 1 (nunca llegan acá).
    try:
        warning = page.get_by_role("dialog").filter(has_text="Invoice total mismatch")
        if warning.count() > 0:
            warning.last.get_by_role("button", name="YES").click()
            log.info("    Descuadre de redondeo (REMAINDER=%.2f <= %.2f) aceptado con YES", remainder, tolerance)
            page.wait_for_timeout(800)
    except Exception:
        pass

    # Señal de guardado = el modal Insert Invoice se cierra (dialogs disminuyen).
    # NO se espera el spinner: queda colgado (bug cosmético del UI) aunque el SAVE
    # haya completado. Polling hasta SAVE_TIMEOUT_MS. En cada vuelta se chequea también si
    # TourplanNX abrió un diálogo de ERROR (ej. falta tipo de cambio) → falla rápido.
    waited = 0
    step = 1000
    while waited < SAVE_TIMEOUT_MS:
        if page.get_by_role("dialog").count() < dialogs_before:
            log.info("    Invoice guardado (SAVE)")
            return
        err = _read_save_error(page)
        if err:
            # TourplanNX rechazó el SAVE con un diálogo de error. Cerrarlo y fallar con el
            # motivo real (no esperar el timeout completo ni loguear "cuelgue").
            _dismiss_save_error(page)
            raise InvoiceSaveError(err)
        page.wait_for_timeout(step)
        waited += step
    # Timeout real sin diálogo de error: volcar diagnóstico y fallar.
    try:
        diag = page.evaluate("""
            () => Array.from(document.querySelectorAll('dialog[open]')).map(d =>
                (d.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120))
        """)
        log.warning("    [DIAG SAVE] dialogs_before=%d, ahora=%d, abiertos=%s",
                    dialogs_before, page.get_by_role("dialog").count(), diag)
    except Exception:
        pass
    raise RuntimeError(
        "El modal de invoice sigue abierto tras SAVE (timeout) — posible cuelgue de CreateAPInvoice"
    )


# Patrones de error de TourplanNX al guardar (diálogo que bloquea el cierre del modal).
_SAVE_ERROR_PATTERNS = ["Error!", "Error adding Invoice", "No currency conversion", "conversion found"]


def _read_save_error(page: Page) -> str | None:
    """Devuelve el texto del diálogo de error tras SAVE (ej. 'Error! 1006 … No currency
    conversion found from CLP to ARS …'), o None si no hay error."""
    return page.evaluate("""
        (patterns) => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            for (const d of dialogs) {
                const t = (d.textContent || '').replace(/\\s+/g, ' ').trim();
                if (patterns.some(p => t.includes(p))) {
                    // Recortar al fragmento del error (desde 'Error')
                    const i = t.indexOf('Error');
                    return (i >= 0 ? t.slice(i) : t).slice(0, 200);
                }
            }
            return null;
        }
    """, _SAVE_ERROR_PATTERNS)


def _dismiss_save_error(page: Page) -> None:
    """Cierra el diálogo de error del SAVE (click en su botón: OK/Close/Exit)."""
    try:
        page.evaluate("""
            (patterns) => {
                const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
                for (const d of dialogs) {
                    const t = (d.textContent || '');
                    if (patterns.some(p => t.includes(p))) {
                        const btn = d.querySelector('button');
                        if (btn) { btn.click(); return; }
                    }
                }
            }
        """, _SAVE_ERROR_PATTERNS)
        page.wait_for_timeout(500)
    except Exception:
        pass


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


def _set_service_date_to(page: Page, value: str) -> bool:
    """Setea el filtro "Service Date To" (tab SELECTION del modal Select Vouchers) para
    NO traer vouchers a futuro. Formato TourplanNX 'DD/Mon/YYYY' (ej '31/Mar/2026').

    Usa el setter nativo + eventos (no .fill(), que rompe el binding del datepicker Angular).

    ESTRUCTURA REAL (verificada en vivo): los campos de fecha vienen en PARES con la MISMA
    clase — 'tpdate-servicedate' aparece 2 veces: el [0] es "Service Date From" y el [1]
    es "Service Date To" (igual que el rango de vouchers usa inputs[0]=FROM, [1]=TO). Se
    identifica SOLO por el token de clase 'tpdate-...' (nunca por el className completo, que
    incluye 'ng-touched' con "to" y hacía matchear mal). Si hay un token 'servicedateto'
    explícito se usa ese; si no, se usa el SEGUNDO 'tpdate-servicedate'. Si no hay ninguno,
    NO setea nada (auto-seguro) y loguea los tokens para diagnóstico.

    Devuelve True solo si encontró y seteó el campo "Service Date To".
    """
    result = page.evaluate("""
        (val) => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            const modal = dialogs[dialogs.length - 1];
            if (!modal) return { set: false, dateTokens: [] };
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            const fire = (el) => {
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            };
            const usable = (el) => el && !el.readOnly && !el.disabled;
            // Tokens tpdate-* de cada input de fecha (ignora ng-*, tpdateinput, etc.).
            const tpdateTokens = (i) => Array.from(i.classList).filter(c => c.startsWith('tpdate-'));
            const dateInputs = Array.from(modal.querySelectorAll('input'))
                .filter(i => tpdateTokens(i).length > 0);
            const dateTokens = dateInputs.map(i => tpdateTokens(i).join(' '));
            const norm = (i) => tpdateTokens(i).map(t => t.replace('tpdate-', '').replace(/-/g, '').toLowerCase());

            // (a) token 'servicedateto' explícito.
            let target = dateInputs.find(i => norm(i).some(t => t === 'servicedateto' || t.includes('servicedateto')));
            // (b) par 'servicedate' → el SEGUNDO (en orden DOM) es el "To".
            if (!target) {
                const pair = dateInputs.filter(i => norm(i).some(t => t === 'servicedate'));
                if (pair.length >= 2) target = pair[1];
            }
            if (usable(target)) {
                fire(target);
                return { set: true, dateTokens, used: tpdateTokens(target).join(' ') };
            }
            return { set: false, dateTokens };
        }
    """, value)
    if result.get("set"):
        log.info("    Service Date To (filtro) = %s [%s]", value, result.get("used", ""))
        return True
    log.warning("    Service Date To: NO se aplicó filtro (no se halló campo). "
                "Tokens de fecha en el modal: %s", result.get("dateTokens", []))
    return False


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


def _read_visible_grid_vouchers(page: Page) -> list[dict]:
    """Lee las filas actualmente renderizadas del grid del modal Select Vouchers.

    El grid VIRTUALIZA: solo monta en el DOM las filas visibles. Devuelve
    [{index, voucher}] de lo que hay montado en este instante."""
    return page.evaluate("""
        () => Array.from(document.querySelectorAll('tr.tpgrid'))
                  .map((row, idx) => {
                      const vc = row.querySelector('td.tpcol-vouchernumber');
                      return vc ? { index: idx, voucher: (vc.textContent || '').replace(/,/g, '').trim() } : null;
                  })
                  .filter(Boolean)
    """) or []


def _scroll_grid_next(page: Page) -> bool:
    """Scrollea el grid del modal Select Vouchers hacia abajo para montar más filas
    (grid virtualizado). Estrategia doble: (1) scrollear el ancestro scrolleable; si no
    se encuentra o no se mueve, (2) traer la ÚLTIMA fila renderizada a la vista
    (scrollIntoView), que fuerza al virtualizador a montar el siguiente bloque.

    Devuelve True si el contenedor se movió (best-effort; el llamador no depende de esto
    para terminar: usa el crecimiento de filas vistas)."""
    return page.evaluate("""
        () => {
            const rows = document.querySelectorAll('tr.tpgrid');
            if (!rows.length) return false;
            const last = rows[rows.length - 1];
            // 1) Ancestro scrolleable (overflow auto/scroll y con contenido de sobra).
            let el = last, cont = null;
            while (el && el.parentElement) {
                el = el.parentElement;
                const oy = getComputedStyle(el).overflowY;
                if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 2) { cont = el; break; }
            }
            let moved = false;
            if (cont) {
                const before = cont.scrollTop;
                // Paso con SOLAPAMIENTO (0.6 del viewport) para no saltear filas que el
                // virtualizador recicla entre lecturas (0.85 dejaba huecos → vouchers no vistos).
                cont.scrollTop = Math.min(cont.scrollTop + Math.floor(cont.clientHeight * 0.6), cont.scrollHeight);
                moved = cont.scrollTop > before + 1;
            }
            // 2) Fallback: traer la última fila a la vista (monta el siguiente bloque).
            if (!moved) {
                try { last.scrollIntoView({ block: 'end', inline: 'nearest' }); } catch (e) {}
            }
            return moved;
        }
    """)


def _scroll_grid_top(page: Page) -> None:
    """Vuelve el grid del modal Select Vouchers al tope (para una segunda pasada que
    recupere filas que la primera no llegó a ver)."""
    page.evaluate("""
        () => {
            const rows = document.querySelectorAll('tr.tpgrid');
            if (!rows.length) return;
            let el = rows[0];
            while (el && el.parentElement) {
                el = el.parentElement;
                const oy = getComputedStyle(el).overflowY;
                if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 2) { el.scrollTop = 0; return; }
            }
        }
    """)


def _read_all_grid_vouchers(page: Page, found: int) -> set[str]:
    """Recorre el grid virtualizado en SOLO LECTURA y devuelve el conjunto de números de
    voucher presentes. No tilda nada (por eso es mucho más rápido y confiable que la
    selección: sin recálculo server-side por tick). 2 pasadas para maximizar cobertura.

    Sirve para decidir si el rango está "limpio" (todos los vouchers son nuestros → SELECT
    ALL seguro) o trae ajenos (→ selección por-voucher)."""
    seen: set[str] = set()
    MAX_STALE = 4
    iters = 0
    max_iters = max(60, found // 3 + 20)
    try:
        page.locator("tr.tpgrid td.tpcol-vouchernumber").first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    for passes in range(2):
        if passes > 0:
            _scroll_grid_top(page)
            page.wait_for_timeout(500)
        stale = 0
        while stale < MAX_STALE and iters < max_iters:
            iters += 1
            before = len(seen)
            for r in _read_visible_grid_vouchers(page):
                seen.add(r["voucher"])
            _scroll_grid_next(page)
            page.wait_for_timeout(350)
            if len(seen) > before:
                stale = 0
            else:
                stale += 1
    log.info("    Lectura del grid: %d números únicos vistos (Found=%d, %d iters)", len(seen), found, iters)
    return seen


def _select_target_vouchers_scrolling(page: Page, target: set[str], found: int) -> list[str]:
    """Recorre el grid virtualizado del modal Select Vouchers tildando SOLO los vouchers
    que están en `target` (los del Excel). Nunca hace SELECT ALL, así no arrastra vouchers
    ajenos que el servidor devuelva dentro del mismo rango.

    Estrategia: leer filas visibles → tildar las que estén en target y falten → scrollear
    → repetir hasta cubrir todos los target o agotar el grid (sin filas nuevas).

    Devuelve la lista de vouchers efectivamente tildados (loaded)."""
    remaining = set(target)
    loaded: list[str] = []
    seen: set[str] = set()
    stale = 0
    MAX_STALE = 4  # scrolls consecutivos sin filas nuevas ni selección → fin
    iters = 0
    # Tope duro de iteraciones: cada scroll debería montar un bloque; con margen amplio.
    max_iters = max(60, found // 3 + 20)

    try:
        page.locator("tr.tpgrid td.tpcol-vouchernumber").first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    try:
        page.locator("tr.tpgrid td.tpcol-checkbox").first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    # Arrancar SIEMPRE desde el tope: la lectura previa del grid (_read_all_grid_vouchers)
    # deja el scroll abajo; sin esto, se empezaría por el final y se verían solo las últimas
    # filas (bug observado: 16/100 con "23 filas vistas").
    _scroll_grid_top(page)
    page.wait_for_timeout(400)

    # Hasta 2 pasadas top→bottom: la 1ª puede saltear filas por reciclado del grid; la 2ª
    # (desde el tope) recupera los target que quedaron sin tildar.
    MAX_PASSES = 2
    for passes in range(MAX_PASSES):
        if not remaining:
            break
        if passes > 0:
            _scroll_grid_top(page)
            page.wait_for_timeout(500)
        stale = 0
        while remaining and stale < MAX_STALE and iters < max_iters:
            iters += 1
            visible = _read_visible_grid_vouchers(page)
            seen_before = len(seen)
            selected_this_pass = 0

            for r in visible:
                v = r["voucher"]
                seen.add(v)
                if v not in remaining:
                    continue
                # Reubicar la fila por número de voucher AL MOMENTO de clickear (el índice
                # puede cambiar entre lectura y click por re-render del grid virtual).
                row_idx = page.evaluate("""
                    (vnum) => {
                        const rows = Array.from(document.querySelectorAll('tr.tpgrid'));
                        for (let i = 0; i < rows.length; i++) {
                            const vc = rows[i].querySelector('td.tpcol-vouchernumber');
                            if (vc && vc.textContent.replace(/,/g, '').trim() === vnum) return i;
                        }
                        return -1;
                    }
                """, v)
                if row_idx < 0:
                    continue
                try:
                    page.locator("tr.tpgrid").nth(row_idx).locator("td.tpcol-checkbox").click(timeout=8000)
                    # NECESARIO: cada tildado dispara un recálculo server-side (spinner) del
                    # total del invoice. Hay que esperarlo antes del próximo tick / de leer el
                    # total; si no, los ticks no se registran y el INVOICE queda subvaluado
                    # (verificado: sin esta espera el total daba ~5x menos → fail-closed erróneo).
                    _wait_for_spinner(page)
                    loaded.append(v)
                    remaining.discard(v)
                    selected_this_pass += 1
                except Exception:
                    pass

            _scroll_grid_next(page)
            page.wait_for_timeout(450)

            # Progreso REAL = tildamos algo o el scroll reveló filas nuevas. Si tras scrollear
            # no aparecen vouchers nuevos MAX_STALE veces, se llegó al fondo del grid.
            if selected_this_pass > 0 or len(seen) > seen_before:
                stale = 0
            else:
                stale += 1

    log.info("    Selección por voucher: %d/%d tildados (Found=%d, %d filas vistas, %d iters, %d pasadas)",
             len(loaded), len(target), found, len(seen), iters, passes + 1)
    return loaded


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

    # 1. Click botón lupa ("Search for Voucher") desde la Invoice Line activa.
    # Usar LUPA_VISIBLE_TIMEOUT (90s) porque el servidor puede tardar en abrir
    # Insert Invoice tras el OK del Create Transaction (especialmente proveedores grandes).
    # Si supera el límite → VoucherSearchTimeout para activar subdivisión del chunk.
    invoice_line = page.get_by_role("dialog").last
    lupa = invoice_line.get_by_role("button", name="Search for Voucher")
    try:
        lupa.wait_for(state="visible", timeout=LUPA_VISIBLE_TIMEOUT)
    except Exception:
        raise VoucherSearchTimeout(
            f"Insert Invoice no abrió en {LUPA_VISIBLE_TIMEOUT // 1000}s — servidor lento"
        )
    lupa.click()
    log.info("    Select Vouchers: abriendo modal (lupa)...")

    # 2. Esperar el modal Select Vouchers
    sv = page.get_by_role("dialog").filter(has_text="Select Vouchers")
    sv.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    # Pausa entre modales: la transición rápida entre ventanas no alcanzaba a montar
    # el modal antes de interactuar. 1s le da tiempo a que cargue.
    page.wait_for_timeout(1000)

    # 3. Setear rango VOUCHER FROM/TO para acotar la búsqueda en el servidor
    if voucher_from or voucher_to:
        _set_voucher_range(page, voucher_from, voucher_to)

    # 3b. Filtro Service Date To: NO traer vouchers a futuro (solo <= fecha de corte).
    if SERVICE_DATE_TO:
        _set_service_date_to(page, SERVICE_DATE_TO)

    # 4. Click SEARCH (button.tpsearch — evita ambigüedad con "Search for Supplier")
    sv.locator("button.tpsearch").click()
    log.info("    SEARCH ejecutado (FROM=%s TO=%s)...", voucher_from, voucher_to)
    # Pausa para que la consulta del servidor traiga los resultados antes de leerlos/seleccionarlos.
    page.wait_for_timeout(1000)

    # 5. Esperar el resultado por el contador 'Found' (NO el spinner, que se cuelga).
    #    _wait_for_search_results lanza VoucherSearchTimeout si hay Error! 1026.
    found = _wait_for_search_results(page)
    log.info("    SEARCH resultados: Found=%d", found)

    # ── Modo masivo: HÍBRIDO. Si el rango es "limpio" (todos los vouchers del grid son
    # nuestros) → SELECT ALL (una operación server-side, total confiable). Si trae ajenos
    # → selección por-voucher. En ambos casos el fail-closed del SAVE valida el total, así
    # que aunque la lectura se saltee algo, nunca se guarda una factura mal.
    if select_all:
        if found <= 0:
            log.warning("    SEARCH sin resultados (Found=0) — saliendo sin cargar")
            try:
                sv.get_by_role("button", name="EXIT").click(force=True)
                sv.wait_for(state="hidden", timeout=MODAL_TIMEOUT)
            except Exception:
                pass
            return {"loaded": [], "not_found": sorted(target)}

        # Leer (solo lectura) qué vouchers hay en el grid para decidir la estrategia.
        grid = _read_all_grid_vouchers(page, found)
        ajenos = grid - target
        present = sorted(grid & target)
        missing = sorted(target - grid)

        if present and not ajenos:
            # Rango limpio: todo lo del grid es nuestro → SELECT ALL confiable.
            log.info("    Rango limpio (%d únicos, 0 ajenos) → SELECT ALL: %d present, %d missing",
                     len(grid), len(present), len(missing))
            sv.locator("button.tpselectall").click()
            try:
                expect(sv.get_by_role("button", name="OK")).to_be_enabled(timeout=MODAL_TIMEOUT)
            except Exception:
                pass
            loaded, not_found = present, missing
        else:
            # Hay vouchers ajenos en el rango (o no se detectó ninguno nuestro): selección
            # por-voucher para no arrastrar ajenos (el fail-closed igual protege).
            if ajenos:
                log.warning("    Rango con %d vouchers ajenos → selección por-voucher (no SELECT ALL)",
                            len(ajenos))
            loaded = _select_target_vouchers_scrolling(page, target, found)
            not_found = sorted(target - set(loaded))

        if not loaded:
            log.warning("    Ningún voucher del Excel para cargar — saliendo sin cargar")
            try:
                sv.get_by_role("button", name="EXIT").click(force=True)
                sv.wait_for(state="hidden", timeout=MODAL_TIMEOUT)
            except Exception:
                pass
            return {"loaded": [], "not_found": sorted(target)}

        try:
            expect(sv.get_by_role("button", name="OK")).to_be_enabled(timeout=MODAL_TIMEOUT)
        except Exception:
            pass
        sv.get_by_role("button", name="OK").click()
        log.info("    OK -> cargando %d present (%d no encontrados)...", len(loaded), len(not_found))
        sv.wait_for(state="hidden", timeout=30000)
        page.wait_for_timeout(800)
        log.info("    Lupa completada (masivo): %d cargados, %d no encontrados", len(loaded), len(not_found))
        return {"loaded": loaded, "not_found": not_found}

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
    # Forzar cierre de cualquier <dialog open> residual via el método nativo del navegador.
    # Angular deja tp-dialog en animación de salida con el atributo open intacto;
    # dialog.close() elimina el atributo y quita la intercepción de pointer events.
    # NO se remueven los tp-dialog del DOM: Angular los gestiona con un pool pre-creado
    # indexado por nth-child; removerlos corrompería ese pool.
    try:
        page.evaluate(
            "() => document.querySelectorAll('dialog[open]').forEach(d => d.close())"
        )
        page.wait_for_timeout(300)
    except Exception:
        pass
    log.info("  abort_transaction: modales cerrados")
