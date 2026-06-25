"""Verbos Playwright para crear cheques (orden de pago) en TourplanNX.

Un cheque agrupa los invoices pendientes de un proveedor en UNA moneda (TourplanNX
solo deja aplicar invoices de la moneda del cheque). El flujo, mapeado en vivo:

  INSERT → modal "Create Transaction" → tab CHEQUE
    REFERENCE        input.tpdescription-transactionreference  → "OP{row_index}{code}"
    CURRENCY         fill_currency() (valor predefinido, igual que invoice)
    PAYMENT DUE DATE input.tpdate-paymentduedate               → fecha del invoice
    PAYMENT TYPE     combo (input siguiente al due date) + .dropdown table tr → "EA1299"
    OK (button.tpok) → modal "Select Invoice Lines"
      button.tpselectall → OK (button.tpok)
    pantalla "Insert Cheque" → SAVE (button.tpsave) → esperar cierre del modal

Reutiliza de modules/transaction_creator.py: fill_currency, abort_transaction, y el
patrón de SAVE (esperar cierre de modal, no el spinner colgado).
"""
from playwright.sync_api import Page, expect

from modules.transaction_creator import (
    fill_currency,
    abort_transaction,
    MODAL_TIMEOUT,
    SAVE_TIMEOUT_MS,
)
from utils.logger import log

REFERENCE_SELECTOR = "input.tpdescription-transactionreference"
CHEQUE_TOTAL_SELECTOR = "input.tpnumber-cheque"
PAYMENT_DUE_DATE_SELECTOR = "input.tpdate-paymentduedate"
# PAYMENT TYPE no tiene clase única; es el input que sigue al due date en el DOM.
PAYMENT_TYPE_XPATH = "xpath=following::input[1]"
CHEQUE_OK = "button.tpok"
CHEQUE_SAVE = "button.tpsave"
SELECT_ALL = "button.tpselectall"


def read_invoice_summary_by_currency(page: Page) -> dict:
    """Lee la grilla de Transactions y devuelve {moneda: {date, total}} de los invoices.

    Recorre las filas `tr.tpgrid` con TYPE="Invoice", agrupa por su CURRENCY y suma
    los AMOUNT (el total por moneda = CHEQUE TOTAL del cheque, para que cuadre el
    REMAINDER). La fecha es la del primer invoice de la moneda; si hay fechas distintas
    deja un warning.
    """
    data = page.evaluate("""
        () => {
            const out = {};
            const num = (s) => parseFloat((s || '0').replace(/,/g, '')) || 0;
            const rows = Array.from(document.querySelectorAll('tr.tpgrid'));
            for (const r of rows) {
                const type = (r.querySelector('td.tpcol-transactiontype')?.textContent || '').trim();
                if (type !== 'Invoice') continue;
                const cur = (r.querySelector('td.tpcol-currency')?.textContent || '').trim();
                const date = (r.querySelector('td.tpcol-date')?.textContent || '').trim();
                const amt = num(r.querySelector('td.tpcol-transactionamount')?.textContent);
                if (!cur || !date) continue;
                if (!(cur in out)) out[cur] = { date, dates: [date], total: 0 };
                else if (!out[cur].dates.includes(date)) out[cur].dates.push(date);
                out[cur].total += amt;
            }
            return out;
        }
    """) or {}
    result = {}
    for cur, info in data.items():
        result[cur] = {"date": info["date"], "total": round(info.get("total", 0), 2)}
        if len(info.get("dates", [])) > 1:
            log.warning("    Moneda %s con invoices de fechas distintas %s — usando %s",
                        cur, info["dates"], info["date"])
    log.info("    Invoices en grilla por moneda: %s",
             ", ".join(f"{c}={v['total']:.2f}@{v['date']}" for c, v in result.items()) or "(ninguno)")
    return result


def _set_date_input(page: Page, selector: str, value: str) -> None:
    """Setea un input de fecha de Angular con el setter nativo (no .fill(), que rompe
    el binding del datepicker)."""
    page.evaluate("""
        ([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            return true;
        }
    """, [selector, value])


def open_cheque_form(page: Page) -> None:
    """Abre Create Transaction (INSERT) y cambia al tab CHEQUE.

    Los tabs son <li> con texto real "Invoice"/"Credit Note"/"Cheque" (capitalizado);
    el UI los muestra en mayúsculas por CSS text-transform, pero Playwright matchea el
    texto del DOM, así que hay que usar "Cheque" (no "CHEQUE").
    """
    page.locator("#creditorview").get_by_role("button", name="INSERT").click()
    dialog = page.get_by_role("dialog").filter(has_text="Create Transaction").last
    expect(dialog.get_by_text("Create Transaction")).to_be_visible(timeout=MODAL_TIMEOUT)
    dialog.get_by_text("Cheque", exact=True).first.click()
    # Señal de que el tab Cheque cargó: CHEQUE TOTAL es exclusivo de ese tab.
    dialog.locator("input.tpnumber-cheque").wait_for(state="visible", timeout=MODAL_TIMEOUT)
    log.info("    Modal cheque abierto (tab CHEQUE)")


def fill_cheque_header(page: Page, reference: str, currency: str, cheque_total: float,
                       payment_due_date: str, payment_type: str) -> None:
    """Rellena REFERENCE, CURRENCY, CHEQUE TOTAL, PAYMENT DUE DATE y PAYMENT TYPE.

    CHEQUE TOTAL = suma de los invoices de la moneda (análogo al EXPECTED TOTAL del
    invoice): con SELECT ALL el REMAINDER queda en 0 y el SAVE no abre el warning de
    descuadre.
    """
    dialog = page.get_by_role("dialog").filter(has_text="Create Transaction").last

    dialog.locator(REFERENCE_SELECTOR).fill(reference)

    fill_currency(page, currency)

    dialog.locator(CHEQUE_TOTAL_SELECTOR).fill(f"{cheque_total:.2f}")
    log.info("    CHEQUE TOTAL=%.2f", cheque_total)

    _set_date_input(page, PAYMENT_DUE_DATE_SELECTOR, payment_due_date)
    log.info("    PAYMENT DUE DATE=%s", payment_due_date)

    # PAYMENT TYPE: combo Angular sin clase única → el input que sigue al due date.
    pt_input = dialog.locator(PAYMENT_DUE_DATE_SELECTOR).locator(PAYMENT_TYPE_XPATH)
    pt_input.click()
    pt_input.fill(payment_type)
    page.wait_for_timeout(1000)
    # Seleccionar la fila del dropdown que matchea el código
    row = page.locator(".dropdown table tr").filter(has_text=payment_type).first
    try:
        row.wait_for(state="visible", timeout=5000)
        row.click()
    except Exception:
        # fallback: ArrowDown + Enter
        pt_input.press("ArrowDown")
        page.wait_for_timeout(300)
        pt_input.press("Enter")
    log.info("    PAYMENT TYPE=%s", payment_type)


def confirm_and_select_invoices(page: Page) -> int:
    """Click OK → modal 'Select Invoice Lines' → SELECT ALL → OK. Vuelve a 'Insert Cheque'.

    Returns:
        Cantidad de invoices (FOUND) aplicados al cheque.
    """
    # El OK del Create Transaction tiene clase tpinvoicelines (no tpok); se ubica por
    # rol/nombre como en confirm_bulk_transaction del pipeline de invoices.
    dialog = page.get_by_role("dialog").filter(has_text="Create Transaction").last
    ok_btn = dialog.get_by_role("button", name="OK")
    expect(ok_btn).to_be_enabled(timeout=MODAL_TIMEOUT)
    ok_btn.click()

    sil = page.get_by_role("dialog").filter(has_text="Select Invoice Lines")
    sil.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    log.info("    Select Invoice Lines abierto")

    sil.locator(SELECT_ALL).click()
    found = _read_found_count(page)
    sil_ok = sil.get_by_role("button", name="OK")
    expect(sil_ok).to_be_enabled(timeout=MODAL_TIMEOUT)
    log.info("    SELECT ALL: %s invoices (FOUND)", found)
    sil_ok.click()
    sil.wait_for(state="hidden", timeout=MODAL_TIMEOUT)
    page.wait_for_timeout(800)
    return found


def _read_found_count(page: Page) -> int:
    """Lee el contador 'Found N' del panel SUMMARY de Select Invoice Lines."""
    return page.evaluate("""
        () => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            const modal = dialogs[dialogs.length - 1];
            if (!modal) return 0;
            const m = (modal.textContent || '').replace(/\\s+/g, ' ').match(/Found[\\s:]*([\\d,]+)/i);
            return m ? parseInt(m[1].replace(/,/g, ''), 10) : 0;
        }
    """) or 0


def save_cheque(page: Page) -> None:
    """Guarda el cheque (Insert Cheque) clickeando SAVE.

    Tras SAVE pueden aparecer dos modales:
      1. "Warning, Cheque total mismatch" (NO/YES) si el REMAINDER != 0 → se confirma YES
         (defensa; con CHEQUE TOTAL = suma de invoices normalmente NO aparece).
      2. "Output Documents" (generar el PDF del cheque) → se cierra con EXIT (el cheque
         ya quedó guardado; no generamos el documento).
    Señal de guardado = el modal Insert Cheque deja de estar abierto.
    """
    dialogs_before = page.get_by_role("dialog").count()
    dialog = page.get_by_role("dialog").last
    save_btn = dialog.locator(CHEQUE_SAVE)
    save_btn.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    save_btn.click(force=True)
    page.wait_for_timeout(1000)

    # 1. Warning de descuadre (si el total no cuadra): confirmar YES.
    try:
        warning = page.get_by_role("dialog").filter(has_text="Cheque total mismatch")
        if warning.count() > 0:
            warning.last.get_by_role("button", name="YES").click()
            log.info("    Warning de descuadre confirmado (YES)")
            page.wait_for_timeout(800)
    except Exception:
        pass

    # 2. Modal "Output Documents": cerrar con EXIT (cheque ya guardado, sin PDF).
    waited = 0
    step = 1000
    while waited < SAVE_TIMEOUT_MS:
        try:
            out_docs = page.get_by_role("dialog").filter(has_text="Output Documents")
            if out_docs.count() > 0:
                out_docs.last.get_by_role("button", name="EXIT").click(force=True)
                log.info("    Output Documents cerrado (cheque guardado sin PDF)")
                page.wait_for_timeout(800)
                return
        except Exception:
            pass
        if page.get_by_role("dialog").count() < dialogs_before:
            log.info("    Cheque guardado (SAVE)")
            return
        page.wait_for_timeout(step)
        waited += step
    raise RuntimeError("El modal de cheque sigue abierto tras SAVE (timeout)")


def create_cheque(page: Page, supplier_code: str, currency: str, reference: str,
                  cheque_total: float, payment_due_date: str, payment_type: str) -> int:
    """Crea un cheque completo para un proveedor+moneda.

    INSERT → tab CHEQUE → header (con CHEQUE TOTAL) → OK → Select Invoice Lines
    (SELECT ALL) → OK → SAVE. Ante cualquier error aborta los modales y propaga.

    Returns:
        Cantidad de invoices aplicados (FOUND).
    """
    log.info("  Creando cheque %s (%s, ref=%s, total=%.2f, due=%s)...",
             supplier_code, currency, reference, cheque_total, payment_due_date)
    try:
        open_cheque_form(page)
        fill_cheque_header(page, reference, currency, cheque_total, payment_due_date, payment_type)
        found = confirm_and_select_invoices(page)
        if found <= 0:
            log.warning("    Cheque %s/%s sin invoices (FOUND=0) — abortando", supplier_code, currency)
            abort_transaction(page)
            return 0
        save_cheque(page)
        log.info("  Cheque %s/%s guardado: %d invoices aplicados", supplier_code, currency, found)
        return found
    except Exception:
        try:
            abort_transaction(page)
        except Exception:
            pass
        raise
