"""Verbos Playwright para crear cheques (orden de pago) en TourplanNX.

Un cheque agrupa los invoices pendientes de un proveedor en UNA moneda (TourplanNX
solo deja aplicar invoices de la moneda del cheque). El flujo, mapeado en vivo:

  INSERT → modal "Create Transaction" → tab CHEQUE
    REFERENCE        input.tpdescription-transactionreference  → "OP{row_index}{code}"
    CURRENCY         fill_currency() (valor predefinido, igual que invoice)
    PAYMENT DUE DATE input.tpdate-paymentduedate               → fecha del invoice
    PAYMENT TYPE     combo (input siguiente al due date) + .dropdown table tr → "EA1299"
    OK (button.tpok) → modal "Select Invoice Lines" (formulario de búsqueda, tabs
                       SELECTION / RESULTS; abre vacío con FOUND=0)
      tab SELECTION → PAYMENT DATE TO (input.tpdate-retailpaymentdateto) = último día del
                      mes siguiente a la fecha del invoice (el default viene = fecha del
                      invoice y acota a 0) → SEARCH (button.tpsearch)
      → grilla carga los invoices → button.tpselectall → OK (button.tpok)
    pantalla "Insert Cheque" → SAVE (button.tpsave) → esperar cierre del modal

Reutiliza de modules/transaction_creator.py: fill_currency, abort_transaction, y el
patrón de SAVE (esperar cierre de modal, no el spinner colgado).
"""
import calendar

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
# El modal Select Invoice Lines es un formulario de búsqueda: hay que ejecutar SEARCH
# para poblar la grilla antes de SELECT ALL (mismo botón que el modal Select Vouchers).
SEARCH_BTN = "button.tpsearch"


def _wait_for_transactions_grid(page: Page, timeout_ms: int = 15000) -> int:
    """Espera a que la grilla de Transactions pinte filas (carga async tras navegar).

    navigate_to_transactions solo espera 500ms fijos; la grilla del servidor puede
    tardar más y se leía vacía, saltando invoices que sí existían. Polling de filas
    tr.tpgrid; si tras el timeout no hay ninguna, se asume grilla genuinamente vacía y
    el caller sigue (no rompe)."""
    waited = 0
    step = 500
    while waited < timeout_ms:
        try:
            rows = page.locator("tr.tpgrid").count()
        except Exception:
            rows = 0
        if rows > 0:
            page.wait_for_timeout(step)  # settle: dejar que termine de poblar
            return rows
        page.wait_for_timeout(step)
        waited += step
    return 0


def read_invoice_summary_by_currency(page: Page) -> dict:
    """Lee la grilla de Transactions y devuelve {moneda: {date, total}} de los invoices.

    Recorre las filas `tr.tpgrid` con TYPE="Invoice", agrupa por su CURRENCY y suma
    los AMOUNT (el total por moneda = CHEQUE TOTAL del cheque, para que cuadre el
    REMAINDER). La fecha es la del primer invoice de la moneda; si hay fechas distintas
    deja un warning.
    """
    # Esperar a que la grilla cargue antes de leer (evita leerla vacía por timing).
    _wait_for_transactions_grid(page)

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


_MESES_CAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _last_day_of_next_month(due_date_str: str) -> str | None:
    """De una fecha 'DD/Mon/YYYY' (ej '19/Jun/2026') devuelve el último día del mes
    SIGUIENTE en el mismo formato (ej '31/Jul/2026'). None si no parsea.

    El campo PAYMENT DATE TO del modal Select Invoice Lines necesita una fecha posterior
    a la de los invoices para traerlos; el último día del mes siguiente da margen."""
    try:
        parts = due_date_str.strip().split("/")
        if len(parts) != 3:
            return None
        month = _MESES_CAP.index(parts[1].strip().title()) + 1  # 'Jun' -> 6
        year = int(parts[2])
    except (ValueError, IndexError, AttributeError):
        return None
    nm, ny = (1, year + 1) if month == 12 else (month + 1, year)  # rollover diciembre
    last = calendar.monthrange(ny, nm)[1]
    return f"{last}/{_MESES_CAP[nm - 1]}/{ny}"


def _set_payment_date_to(page: Page, value: str) -> None:
    """Setea el filtro PAYMENT DATE TO (input.tpdate-retailpaymentdateto) del formulario
    SELECTION de Select Invoice Lines, en el último dialog abierto.

    Por defecto ese filtro viene = PAYMENT DUE DATE del cheque (la fecha del invoice), lo
    que acota la búsqueda y devuelve 0 invoices. Poniéndolo en el último día del mes
    siguiente, SEARCH trae los invoices. Usa el setter nativo + eventos (no .fill(), que
    rompe el binding del datepicker Angular). NO toca la fecha del cheque (solo el filtro)."""
    page.evaluate("""
        (val) => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            const modal = dialogs[dialogs.length - 1];
            if (!modal) return;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            modal.querySelectorAll('input.tpdate-retailpaymentdateto').forEach(el => {
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            });
        }
    """, value)


def _activate_selection_tab(page: Page, attempts: int = 4, per_attempt_ms: int = 4000) -> bool:
    """Activa la tab SELECTION del modal Select Invoice Lines y espera a que el botón
    SEARCH sea visible (vive en esa tab). El click de tab a veces no registra al primer
    intento (race Angular), dejando RESULTS activa y SEARCH oculto → se reintenta. Usa
    click nativo sobre el último dialog abierto. Devuelve True si SEARCH quedó visible."""
    for _ in range(attempts):
        page.evaluate(
            "() => { const d = Array.from(document.querySelectorAll('dialog[open]')).pop();"
            " if (!d) return;"
            " const li = Array.from(d.querySelectorAll('li.tptablabel'))"
            "   .find(e => (e.textContent || '').trim() === 'Selection');"
            " if (li) li.click(); }"
        )
        waited = 0
        while waited < per_attempt_ms:
            # Visibilidad REAL (no solo bounding box): la tab inactiva usa visibility:hidden,
            # que da rect>0 pero Playwright (y un click real) lo trata como no visible.
            search_visible = page.evaluate(
                "() => { const d = Array.from(document.querySelectorAll('dialog[open]')).pop();"
                " const b = d && d.querySelector('button.tpsearch');"
                " if (!b) return false;"
                " const r = b.getBoundingClientRect();"
                " const st = window.getComputedStyle(b);"
                " return r.width > 0 && r.height > 0 && b.offsetParent !== null"
                "        && st.visibility !== 'hidden' && st.display !== 'none'; }"
            )
            if search_visible:
                return True
            page.wait_for_timeout(300)
            waited += 300
    return False


def _select_all_and_wait_ok(page: Page, attempts: int = 4, per_attempt_ms: int = 5000) -> bool:
    """Clickea SELECT ALL y espera a que el OK del modal se habilite.

    La selección a veces no registra al primer click si la grilla no terminó de asentarse
    (OK queda disabled). Se reintenta el click (nativo, sobre el último dialog abierto —
    el método que funciona; SELECT ALL queda disabled cuando la selección sí registró, así
    que el re-click solo ocurre si hizo falta). Devuelve True si OK quedó habilitado."""
    for _ in range(attempts):
        page.evaluate(
            "() => { const d = Array.from(document.querySelectorAll('dialog[open]')).pop();"
            " const b = d && d.querySelector('button.tpselectall');"
            " if (b && !b.disabled) b.click(); }"
        )
        waited = 0
        while waited < per_attempt_ms:
            ok_enabled = page.evaluate(
                "() => { const d = Array.from(document.querySelectorAll('dialog[open]')).pop();"
                " const ok = d && d.querySelector('button.tpok');"
                " return ok ? !ok.disabled : false; }"
            )
            if ok_enabled:
                return True
            page.wait_for_timeout(400)
            waited += 400
    return False


def confirm_and_select_invoices(page: Page, payment_due_date: str) -> int:
    """Click OK → modal 'Select Invoice Lines' → SELECT ALL → OK. Vuelve a 'Insert Cheque'.

    Args:
        payment_due_date: fecha del invoice ('DD/Mon/YYYY'). Se usa solo para calcular el
            filtro PAYMENT DATE TO (último día del mes siguiente); NO modifica la fecha
            del cheque.

    Returns:
        Cantidad de invoices (FOUND) aplicados al cheque.
    """
    # El OK del Create Transaction tiene clase tpinvoicelines (no tpok); se ubica por
    # rol/nombre como en confirm_bulk_transaction del pipeline de invoices.
    dialog = page.get_by_role("dialog").filter(has_text="Create Transaction").last
    ok_btn = dialog.get_by_role("button", name="OK")
    expect(ok_btn).to_be_enabled(timeout=MODAL_TIMEOUT)
    ok_btn.click()

    sil = page.get_by_role("dialog").filter(has_text="Select Invoice Lines").last
    sil.wait_for(state="visible", timeout=MODAL_TIMEOUT)
    log.info("    Select Invoice Lines abierto")

    # El modal tiene tabs SELECTION / RESULTS. Abre mostrando RESULTS vacío ("No results
    # found", FOUND=0). El botón Search vive en la tab SELECTION (formulario de criterios);
    # con RESULTS activa, button.tpsearch existe en el DOM pero NO es visible. La activación
    # de la tab a veces no registra al primer click (race Angular) → _activate_selection_tab
    # reintenta hasta que SEARCH quede visible.
    if not _activate_selection_tab(page):
        log.warning("    No se pudo activar la tab SELECTION (SEARCH no visible) — abortando")
        _dump_modal_state(page, "cheque_selection_tab_no_activa")
        return 0

    # PAYMENT DATE TO: por defecto viene = fecha del invoice y acota la búsqueda a 0.
    # Se setea al último día del mes siguiente para que SEARCH traiga los invoices
    # (solo el filtro; la fecha del cheque queda intacta).
    payment_date_to = _last_day_of_next_month(payment_due_date)
    if payment_date_to:
        _set_payment_date_to(page, payment_date_to)
        page.wait_for_timeout(300)
        log.info("    PAYMENT DATE TO (filtro) = %s", payment_date_to)
    else:
        log.warning("    No se pudo calcular PAYMENT DATE TO desde %r — se busca con el default",
                    payment_due_date)

    # Ejecutar SEARCH (click nativo sobre el último dialog abierto, consistente con la
    # activación de la tab: evita el chequeo de visibilidad de Playwright que falla en el
    # primer modal). Sin SEARCH la grilla queda vacía y SELECT ALL/OK deshabilitados.
    page.evaluate(
        "() => { const d = Array.from(document.querySelectorAll('dialog[open]')).pop();"
        " const b = d && d.querySelector('button.tpsearch'); if (b) b.click(); }"
    )
    log.info("    SEARCH ejecutado en Select Invoice Lines")

    # Tras SEARCH la grilla se puebla async; esperar a que cargue (contador 'Found' o
    # filas) antes de SELECT ALL, sin wait fijo (racy).
    select_all_btn = sil.locator(SELECT_ALL)
    loaded = _wait_for_invoices_loaded(page)
    log.info("    Grilla cargada: Found=%s", loaded)

    if loaded <= 0:
        # El modal abrió pero la grilla no trajo invoices tras esperar (SELECT ALL queda
        # disabled). Se captura el DOM del modal para diagnosticar y se aborta limpio:
        # clickear el botón deshabilitado solo agrega un timeout de 30s y una excepción.
        _dump_modal_state(page, "cheque_select_invoice_lines_vacio")
        return 0

    expect(select_all_btn).to_be_enabled(timeout=MODAL_TIMEOUT)
    # Asentar la grilla antes de seleccionar: si se clickea SELECT ALL apenas Found>0,
    # a veces la selección no registra y OK nunca se habilita (race observado: 1GIAG1
    # con 172 invoices alcanzaba a asentarse, 1ALT05 con menos no). _select_all_and_wait_ok
    # reintenta hasta que OK quede habilitado.
    page.wait_for_timeout(800)
    if not _select_all_and_wait_ok(page):
        log.warning("    OK no se habilitó tras SELECT ALL (selección no registró) — abortando")
        _dump_modal_state(page, "cheque_ok_no_habilita")
        return 0
    found = _read_found_count(page) or loaded
    log.info("    SELECT ALL: %s invoices (FOUND)", found)
    sil.get_by_role("button", name="OK").click()
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


def _wait_for_invoices_loaded(page: Page, timeout_ms: int = 35000) -> int:
    """Espera a que Select Invoice Lines termine de cargar los invoices del servidor.

    El modal abre antes de que el servidor responda, así que se hace polling (no wait
    fijo, que es racy). Señal de carga: el contador 'Found' > 0, o filas en la grilla
    del modal. Espejo de _wait_for_search_results del pipeline de invoices (que también
    espera el 'Found' en vez del spinner, que se cuelga). Devuelve el conteo (0 si no
    apareció ninguna señal dentro del timeout — el caller igual sigue, no asume vacío)."""
    waited = 0
    step = 500
    while waited < timeout_ms:
        found = _read_found_count(page)
        if found and found > 0:
            return found
        try:
            rows = page.locator("dialog[open] tr.tpgrid").count()
        except Exception:
            rows = 0
        if rows > 0:
            # La grilla ya pintó filas; dar un instante a que el contador 'Found' actualice.
            page.wait_for_timeout(step)
            return _read_found_count(page) or rows
        page.wait_for_timeout(step)
        waited += step
    return _read_found_count(page) or 0


def _dump_modal_state(page: Page, name: str) -> None:
    """Captura screenshot + HTML del último dialog abierto para diagnosticar fallas.

    Se usa cuando la grilla queda vacía (FOUND=0): deja en outputs/screenshots/ una
    foto y un recorte del DOM del modal, para entender qué devolvió el servidor sin
    tener que reproducir el flujo en producción a mano."""
    from config.settings import SCREENSHOT_DIR
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.png"))
        info = page.evaluate(
            "() => {"
            " const d = Array.from(document.querySelectorAll('dialog[open]'));"
            " const m = d.length ? d[d.length - 1] : null;"
            " if (!m) return {buttons: [], inputs: []};"
            " const buttons = Array.from(m.querySelectorAll('button')).map(b => ({"
            "   cls: b.className, txt: (b.textContent || '').trim().slice(0, 30),"
            "   disabled: b.disabled}));"
            " const inputs = Array.from(m.querySelectorAll('input')).map(i => ({"
            "   cls: i.className, ph: i.placeholder || '', val: i.value || ''}));"
            " return {buttons, inputs};"
            "}"
        )
        log.warning("    [diag] Modal '%s' botones: %s", name, info.get("buttons"))
        log.warning("    [diag] Modal '%s' inputs: %s", name, info.get("inputs"))
    except Exception as e:
        log.debug("    [diag] No se pudo capturar el estado del modal: %s", e)


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
        found = confirm_and_select_invoices(page, payment_due_date)
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
