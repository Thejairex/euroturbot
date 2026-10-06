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


class ReferenceExistsError(Exception):
    """La REFERENCE del cheque ya existe en TourplanNX (Error 1038 Transaction error,
    Reference Exists) — create_cheque la captura y reintenta con otra referencia."""


# SAVE_TIMEOUT_MS (120s, de modules/transaction_creator.py) alcanza para los chunks de
# ~200 vouchers del pipeline de invoices, pero no para cheques con selecciones enormes
# — confirmado en vivo (2026-09-30, 1ING01): 21.555 invoices seleccionados, SAVE seguía
# procesando pasados los 120s. Constante propia, no se toca la compartida (no vale la
# pena hacer esperar 10min a un invoice normal que realmente falló).
CHEQUE_SAVE_TIMEOUT_MS = 600000


def _open_transaction_filters(page: Page, attempts: int = 4, per_attempt_ms: int = 1500) -> bool:
    """Expande el panel 'Transaction Filters' de la grilla de Transactions si está
    colapsado (así viene tras navigate_to_transactions). Idempotente: si ya está
    expandido (clase 'tpexpanded' en vez de 'tpcollapsed') no hace nada.

    El texto del label se ve "TRANSACTION FILTERS" en mayúsculas por CSS
    (text-transform), pero el DOM real tiene "Transaction Filters" — mismo motivo por
    el que el resto del código nunca usa exact=True/mayúsculas fijas para estos labels
    (ver reactivate_creditor). El toggle solo reacciona al click sobre el ícono
    (i.groupicon), no sobre el texto en sí (confirmado en vivo: clickear el texto no
    cambia la clase tpcollapsed/tpexpanded).

    Reintenta y VERIFICA que la clase haya cambiado a 'tpexpanded' antes de seguir —
    el click a veces no registra al primer intento (mismo race de Angular ya visto en
    _activate_selection_tab). Sin esta verificación, un solo click que no registra deja
    el resto de la corrida rota: este componente NO se destruye entre proveedores (
    persiste en la sesión), así que si _set_currency_filter asume que ya está expandido
    y no lo está, el input de CURRENCY queda oculto y sus clicks fallan para TODOS los
    proveedores siguientes de la misma corrida — confirmado en vivo (2026-09-28): un
    batch de 27 proveedores dio "(ninguno)" en el 100% de los casos por este motivo,
    incluidos proveedores con cientos de invoices reales conocidos.

    Devuelve True si el panel quedó expandido (o ya lo estaba), False si no se pudo
    confirmar tras todos los intentos (el caller decide si seguir igual).

    Click nativo por JS (page.evaluate + icon.click()), NO Playwright .click(force=True)
    — confirmado en vivo (2026-09-30, 1ING01): un <dialog> vacío del pool de Angular
    puede quedar posicionado ENCIMA del ícono (document.elementFromPoint() ahí devuelve
    el dialog, no el ícono). .click(force=True) de Playwright salta el chequeo de
    actionability pero sigue disparando un evento de mouse en esas coordenadas reales,
    así que el navegador se lo entrega al dialog que está arriba, no al ícono — el click
    nunca llega. El .click() nativo de JS invoca el handler directo sobre el elemento,
    sin pasar por hit-testing del navegador, así que funciona igual aunque algo lo tape
    visualmente (mismo motivo por el que click_hamburger ya usa este patrón)."""
    group = page.locator("tp-group[tptype='TransactionFilterGroup']").first
    for _ in range(attempts):
        cls = group.get_attribute("class") or ""
        if "tpcollapsed" not in cls:
            return True
        page.evaluate("""
            () => {
                const g = document.querySelector("tp-group[tptype='TransactionFilterGroup']");
                const icon = g && g.querySelector('i.groupicon');
                if (icon) icon.click();
            }
        """)
        waited = 0
        while waited < per_attempt_ms:
            page.wait_for_timeout(200)
            waited += 200
            if "tpcollapsed" not in (group.get_attribute("class") or ""):
                return True
    return False


def _wait_for_currency_grid(page: Page, currency: str, timeout_ms: int = 10000) -> None:
    """Espera a que la grilla refleje la moneda pedida tras cambiar el filtro CURRENCY.

    No alcanza con esperar 'hay filas': si la moneda anterior tenía la misma cantidad de
    filas (o más), se podría leer la grilla vieja antes de que Angular termine de
    re-renderizar. Se poll ea hasta que la PRIMERA fila sea de la moneda pedida, o hasta
    timeout (grilla genuinamente vacía para esa moneda — el caller igual filtra por
    moneda al leer, así que una fila vieja que quede no contamina el resultado)."""
    waited = 0
    step = 400
    while waited < timeout_ms:
        state = page.evaluate(
            """(cur) => {
                const rows = Array.from(document.querySelectorAll('tr.tpgrid'));
                if (rows.length === 0) return 'empty';
                const firstCur = (rows[0].querySelector('td.tpcol-currency')?.textContent || '').trim();
                return firstCur === cur ? 'match' : 'stale';
            }""",
            currency,
        )
        if state == "match":
            page.wait_for_timeout(300)
            return
        page.wait_for_timeout(step)
        waited += step


def _native_click(locator, timeout_ms: int = 8000) -> None:
    """Click nativo por JS (locator.evaluate("el => el.click()")) en vez de
    Locator.click() de Playwright.

    Confirmado en vivo (2026-09-30, 1EURO1): un <tp-spinner> con un <dialog> vacío
    puede quedar flotando ENCIMA de estos inputs/botones mientras la cuenta todavía está
    cargando datos en segundo plano ("dialog ... subtree intercepts pointer events").
    Locator.click() dispara un evento de mouse en coordenadas reales, que el navegador
    entrega a lo que esté arriba (el spinner), no al elemento — se cuelga 8s reintentando
    para nada. .evaluate() invoca el handler directo sobre el elemento que Playwright ya
    resolvió, sin pasar por hit-testing del navegador (mismo patrón que
    _open_transaction_filters usa para el ícono del panel)."""
    locator.first.evaluate("el => el.click()", timeout=timeout_ms)


def _get_available_currencies(page: Page) -> list[str]:
    """Abre el dropdown de CURRENCY y devuelve las monedas que el dropdown realmente
    lista para ESTE proveedor (además de "ALL"), sin seleccionar ninguna.

    El dropdown NO es una lista fija global — solo lista las monedas que el proveedor
    tiene (confirmado en vivo, 2026-09-28: 1ACUS1 solo tenía "ALL"/"ARS", nunca "USD").
    Iterar ciegamente sobre FILTER_CURRENCIES hacía que _set_currency_filter esperara 8s
    por una fila que nunca iba a aparecer para la moneda ausente — no era un error, era
    la respuesta correcta del sistema. Leer las opciones reales evita esa espera inútil
    y la falsa categorización como "falla"."""
    currency_input = page.get_by_text("Currency", exact=True).first.locator(
        "xpath=following::input[1]"
    )
    _native_click(currency_input)
    page.wait_for_timeout(400)
    texts = page.locator(".dropdown table tr:visible").all_inner_texts()
    currencies = []
    for t in texts:
        code = t.strip().split()[0] if t.strip() else ""
        if code and code != "ALL" and code not in currencies:
            currencies.append(code)
    # Cerrar el dropdown sin cambiar nada: seleccionar "ALL" (siempre presente) es
    # inocuo, ya que el próximo _set_currency_filter va a sobreescribir el filtro igual.
    _native_click(page.locator(".dropdown table tr:visible").filter(has_text="ALL"))
    page.wait_for_timeout(300)
    _native_click(page.get_by_role("button", name="OK"))
    return currencies


def _set_currency_filter(page: Page, currency: str) -> None:
    """Filtra la grilla de Transactions por una moneda específica (panel TRANSACTION
    FILTERS) y ejecuta OK.

    Confirmado en vivo (1SHE14, 2026-09-28): con CURRENCY='ALL - ALL' (el default tras
    navegar) la grilla mostraba 17 invoices ARS y CERO USD — pero filtrando
    explícitamente por USD aparecían 42 invoices reales (32 nunca cobrados) + 10 cheques
    ya aplicados, invisibles en la vista 'ALL'. Por eso hay que pedir cada moneda por
    separado en vez de confiar en 'ALL'.
    """
    # "Currency" (no "CURRENCY": ver _open_transaction_filters). .first toma el label
    # del panel de filtros, que precede en el DOM al header de la misma columna en la
    # grilla (confirmado en vivo: ambos "Currency" quedan visibles simultáneamente).
    currency_input = page.get_by_text("Currency", exact=True).first.locator(
        "xpath=following::input[1]"
    )
    _native_click(currency_input)
    page.wait_for_timeout(300)
    # ":visible" — no alcanza con ".dropdown table tr" a secas: ".dropdown" es una clase
    # genérica reusada por otros combos de la página (búsqueda de proveedor, PAYMENT
    # TYPE), y una instancia vieja oculta del pool de Angular puede seguir matcheando el
    # selector sin tener "ARS"/"USD" en su texto — confirmado en vivo (2026-09-28, ~14
    # proveedores distintos): Locator.click quedaba 30s esperando una fila que nunca
    # iba a aparecer porque el dropdown real todavía no estaba en el DOM o el que
    # matcheaba primero era el viejo.
    row = page.locator(".dropdown table tr:visible").filter(has_text=currency)
    _native_click(row)
    page.wait_for_timeout(300)
    _native_click(page.get_by_role("button", name="OK"))
    _wait_for_currency_grid(page, currency)


def read_invoice_summary_by_currency(page: Page, supplier_code: str) -> dict:
    """Lee la grilla de Transactions y devuelve {moneda: {date, total}} de los invoices
    QUE NOSOTROS CARGAMOS (no todo lo que TourplanNX muestre como pendiente).

    Pide cada moneda que el proveedor realmente tiene (ver _get_available_currencies)
    por separado vía el filtro CURRENCY (ver _set_currency_filter) en vez de leer el
    default 'ALL - ALL', que en TourplanNX oculta todas las monedas salvo una. Para cada
    moneda: recorre las filas `tr.tpgrid` con TYPE="Invoice", esa moneda exacta (evita
    contaminación por filas viejas que no hayan terminado de refrescar), Y cuya
    REFERENCE (columna td.tpcol-transactionreference) contenga supplier_code — nuestro
    pipeline de invoices siempre genera la REFERENCE como "INV{row_index}{supplier_code}"
    (ver modules/transaction_creator.py), así que ese substring identifica de forma
    confiable las facturas que cargamos nosotros, sin tocar historial previo de la
    cuenta. Confirmado en vivo (2026-09-30, 1ING01): sin este filtro, la búsqueda traía
    21.555 invoices pendientes de los que solo 75 eran nuestros — el resto, años de
    historial acumulado que no nos corresponde pagar en una corrida automática.

    Suma los AMOUNT (el total = CHEQUE TOTAL del cheque). La fecha es la del primer
    invoice; si hay fechas distintas deja un warning.
    """
    if not _open_transaction_filters(page):
        # No se pudo confirmar que el panel quedó expandido tras varios intentos: seguir
        # igual dejaría el input de CURRENCY oculto y cada _set_currency_filter fallaría
        # por 8s por moneda para nada. Mejor cortar acá con un resultado vacío explícito
        # (el caller lo trata igual que "sin invoices en la grilla") que arriesgar dejar
        # el componente en un estado raro para el resto de la corrida.
        log.warning("    No se pudo expandir Transaction Filters — sin datos de moneda para este proveedor")
        return {}

    try:
        currencies = _get_available_currencies(page)
    except Exception as e:
        log.warning("    No se pudo leer las monedas disponibles del filtro: %s", e)
        return {}

    result: dict[str, dict] = {}
    for currency in currencies:
        try:
            _set_currency_filter(page, currency)
        except Exception as e:
            # Que falle el filtro de UNA moneda (ej. dropdown que no respondió a
            # tiempo) no debe perder la otra moneda del mismo proveedor — se salta y
            # se sigue con la próxima en vez de abortar toda la lectura.
            log.warning("    No se pudo filtrar por moneda %s — se salta: %s", currency, e)
            continue

        data = page.evaluate(
            """([cur, code]) => {
                const num = (s) => parseFloat((s || '0').replace(/,/g, '')) || 0;
                const rows = Array.from(document.querySelectorAll('tr.tpgrid'));
                const out = { dates: [], total: 0 };
                for (const r of rows) {
                    const type = (r.querySelector('td.tpcol-transactiontype')?.textContent || '').trim();
                    if (type !== 'Invoice') continue;
                    const rowCur = (r.querySelector('td.tpcol-currency')?.textContent || '').trim();
                    if (rowCur !== cur) continue;
                    const ref = (r.querySelector('td.tpcol-transactionreference')?.textContent || '').trim();
                    if (!ref.includes(code)) continue;
                    const date = (r.querySelector('td.tpcol-date')?.textContent || '').trim();
                    const amt = num(r.querySelector('td.tpcol-transactionamount')?.textContent);
                    if (!date) continue;
                    if (!out.dates.includes(date)) out.dates.push(date);
                    out.total += amt;
                }
                return out;
            }""",
            [currency, supplier_code],
        ) or {"dates": [], "total": 0}

        if data.get("dates"):
            dates_sorted = sorted(data["dates"], key=lambda d: _parse_tp_date(d) or (0, 0, 0))
            earliest, latest = dates_sorted[0], dates_sorted[-1]
            # search_until debe cubrir la factura MÁS NUEVA de la moneda, no la primera
            # encontrada: si el proveedor tiene invoices de meses distintos (cargados en
            # corridas separadas), acotar la ventana de búsqueda de Select Invoice Lines
            # a partir de la primera fecha deja afuera las más nuevas (ver
            # confirm_and_select_invoices) — confirmado en vivo con 1SHE14 (2026-09-28):
            # invoices de junio y septiembre, search_until calculado solo desde junio
            # daba FOUND=0 para todo, septiembre incluido.
            result[currency] = {
                "date": earliest,
                "search_until": _last_day_of_next_month(latest) or latest,
                "total": round(data.get("total", 0), 2),
            }
            if len(dates_sorted) > 1:
                log.warning("    Moneda %s con invoices de fechas distintas %s — due=%s, "
                            "búsqueda hasta %s", currency, dates_sorted, earliest,
                            result[currency]["search_until"])

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


def _parse_tp_date(date_str: str) -> tuple[int, int, int] | None:
    """Parsea 'DD/Mon/YYYY' a (year, month, day) para poder ordenar/comparar fechas
    de TourplanNX. None si no parsea."""
    try:
        parts = date_str.strip().split("/")
        if len(parts) != 3:
            return None
        day = int(parts[0])
        month = _MESES_CAP.index(parts[1].strip().title()) + 1
        year = int(parts[2])
        return (year, month, day)
    except (ValueError, IndexError, AttributeError):
        return None


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
            # NO se chequea offsetParent: un <dialog> nativo mostrado con showModal() vive
            # en el "top layer" del navegador y SIEMPRE tiene offsetParent === null aunque
            # esté perfectamente visible — confirmado en vivo (2026-09-28) como falso
            # negativo que colgaba 30s con el modal ya abierto (ver
            # _wait_for_select_invoice_lines_visible, mismo bug).
            search_visible = page.evaluate(
                "() => { const d = Array.from(document.querySelectorAll('dialog[open]')).pop();"
                " const b = d && d.querySelector('button.tpsearch');"
                " if (!b) return false;"
                " const r = b.getBoundingClientRect();"
                " const st = window.getComputedStyle(b);"
                " return r.width > 0 && r.height > 0"
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


def _wait_for_select_invoice_lines_visible(page: Page, timeout_ms: int | None = None) -> bool:
    """Polling nativo: True apenas algún <dialog open> con texto 'Select Invoice Lines'
    esté REALMENTE visible (bounding box > 0, visibility/display), no solo presente en
    el DOM. Ver comentario en confirm_and_select_invoices.

    NO se chequea offsetParent: confirmado en vivo (2026-09-28, supplier TEMP) que un
    <dialog> nativo mostrado con showModal() vive en el "top layer" del navegador y
    SIEMPRE tiene offsetParent === null (w=1728 h=810, visibility=visible,
    display=block, offsetParent=null) — con ese chequeo esta función daba falso
    negativo SIEMPRE para este modal, colgando 30s en cada cheque aunque ya estuviera
    abierto y listo para usar."""
    timeout_ms = timeout_ms if timeout_ms is not None else MODAL_TIMEOUT
    waited = 0
    step = 300
    while waited < timeout_ms:
        visible = page.evaluate("""
            () => {
                const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
                return dialogs.some(d => {
                    if (!(d.textContent || '').includes('Select Invoice Lines')) return false;
                    const r = d.getBoundingClientRect();
                    const st = window.getComputedStyle(d);
                    return r.width > 0 && r.height > 0
                           && st.visibility !== 'hidden' && st.display !== 'none';
                });
            }
        """)
        if visible:
            return True
        page.wait_for_timeout(step)
        waited += step
    return False


def _check_and_dismiss_transaction_error(page: Page) -> str | None:
    """Busca un dialog con texto "Transaction error" (ej. "Error! 1038 Transaction
    error. Error is Reference Exists"), lo cierra con su botón, y devuelve el texto si
    lo encontró (None si no había ninguno).

    TourplanNX puede tirar este error en MÁS DE UN punto del flujo del cheque — no solo
    al abrir Select Invoice Lines, sino también recién al hacer SAVE final (confirmado
    en vivo, 2026-09-30, 1CATP1: la referencia ya existía de una prueba anterior del
    mismo día, y el error apareció DESPUÉS de "Warning de descuadre confirmado", en el
    paso de guardado — save_cheque no lo reconocía y esperaba los 10 minutos completos
    del timeout para nada). Por eso esta función es compartida entre
    confirm_and_select_invoices y save_cheque, no exclusiva de un solo punto."""
    error_text = page.evaluate("""
        () => {
            const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
            const err = dialogs.find(d => (d.textContent || '').includes('Transaction error'));
            return err ? (err.textContent || '').replace(/\\s+/g, ' ').trim() : null;
        }
    """)
    if error_text:
        page.evaluate("""
            () => {
                const dialogs = Array.from(document.querySelectorAll('dialog[open]'));
                const err = dialogs.find(d => (d.textContent || '').includes('Transaction error'));
                const btn = err && err.querySelector('button');
                if (btn) btn.click();
            }
        """)
        page.wait_for_timeout(500)
    return error_text


def confirm_and_select_invoices(page: Page, payment_due_date: str, supplier_code: str,
                                search_until_date: str | None = None) -> tuple[int, int]:
    """Click OK → modal 'Select Invoice Lines' → SELECT ALL → OK. Vuelve a 'Insert Cheque'.

    Se probó tildar checkboxes de a tandas para no seleccionar 1000+ invoices de una,
    pero esta grilla resultó ser de selección ÚNICA por fila (ver create_cheque) — no es
    viable. Se usa siempre SELECT ALL, que soporta selecciones grandes sin problema
    (1AER01/USD: 1094 invoices aplicados en una sola corrida).

    Filtra por "Reference Contains" = supplier_code ANTES de buscar: sin esto, SELECT ALL
    trae TODO lo que TourplanNX considere pendiente para esa moneda, incluido historial
    de años previo a este pipeline — confirmado en vivo (2026-09-30, 1ING01): 21.555
    invoices encontrados, de los cuales solo 75 eran nuestros (el resto, acumulado
    histórico que no nos corresponde pagar en una corrida automática). Nuestro pipeline
    de invoices siempre genera la REFERENCE como "INV{row_index}{supplier_code}" (ver
    modules/transaction_creator.py), así que ese substring alcanza para acotar la
    búsqueda a solo lo que nosotros cargamos.

    Args:
        payment_due_date: fecha del invoice ('DD/Mon/YYYY'). Fallback para calcular el
            filtro PAYMENT DATE TO si no se pasa search_until_date; NO modifica la fecha
            del cheque.
        search_until_date: valor ya calculado para el filtro PAYMENT DATE TO (ver
            read_invoice_summary_by_currency: debe cubrir la factura MÁS NUEVA de la
            moneda, no solo la primera). Si no se pasa, cae a
            _last_day_of_next_month(payment_due_date) (comportamiento previo).

    Returns:
        (found, loaded): invoices efectivamente aplicados (SELECT ALL, así que found ==
        loaded salvo que algo falle) y el total que la búsqueda encontró.
    """
    # El OK del Create Transaction tiene clase tpinvoicelines (no tpok); se ubica por
    # rol/nombre como en confirm_bulk_transaction del pipeline de invoices.
    dialog = page.get_by_role("dialog").filter(has_text="Create Transaction").last
    ok_btn = dialog.get_by_role("button", name="OK")
    expect(ok_btn).to_be_enabled(timeout=MODAL_TIMEOUT)
    ok_btn.click()

    # TourplanNX puede responder con un error en vez de abrir Select Invoice Lines (ej.
    # "Error! 1038 Transaction error. Error is Reference Exists" cuando la REFERENCE ya
    # existe) — confirmado en vivo (2026-09-30): sin este chequeo, el código esperaba
    # 30-60s a un modal que nunca iba a aparecer y terminaba reportando "sin invoices"
    # (FOUND=0), escondiendo el error real. Se detecta por el texto "Transaction error"
    # y se cierra el dialog para no dejarlo bloqueando el resto del flujo.
    page.wait_for_timeout(500)
    error_text = _check_and_dismiss_transaction_error(page)
    if error_text:
        if "Reference Exists" in error_text:
            raise ReferenceExistsError(error_text)
        raise RuntimeError(f"TourplanNX rechazó la transacción: {error_text}")

    # Espera de VISIBILIDAD REAL (no solo presencia en el DOM) por polling nativo, no
    # sil.wait_for(state="visible") de Playwright: en sesiones largas (cientos de
    # proveedores seguidos) el pool de <dialog> de Angular puede dejar más de una
    # instancia con el texto "Select Invoice Lines" (una vieja oculta de una iteración
    # anterior), y get_by_role(...).filter(has_text=...).last podía terminar apuntando a
    # la instancia equivocada, colgando 30s+ aunque el modal correcto ya estuviera abierto
    # — mismo patrón ya resuelto para otros diálogos en este archivo (ver
    # _activate_selection_tab).
    # 60s (no MODAL_TIMEOUT=30s default): proveedores con años de historial (ej. 1AER01,
    # rango de fechas 2021-2026) tardan más en que Angular termine de armar el modal —
    # confirmado en vivo (2026-09-29) fallando consistentemente a los 30s.
    if not _wait_for_select_invoice_lines_visible(page, timeout_ms=60000):
        log.warning("    Select Invoice Lines no llegó a abrirse visible — abortando")
        _dump_modal_state(page, "cheque_select_invoice_lines_no_abre")
        return 0, 0
    sil = page.get_by_role("dialog").filter(has_text="Select Invoice Lines").last
    log.info("    Select Invoice Lines abierto")

    # El modal tiene tabs SELECTION / RESULTS. Abre mostrando RESULTS vacío ("No results
    # found", FOUND=0). El botón Search vive en la tab SELECTION (formulario de criterios);
    # con RESULTS activa, button.tpsearch existe en el DOM pero NO es visible. La activación
    # de la tab a veces no registra al primer click (race Angular) → _activate_selection_tab
    # reintenta hasta que SEARCH quede visible.
    if not _activate_selection_tab(page):
        log.warning("    No se pudo activar la tab SELECTION (SEARCH no visible) — abortando")
        _dump_modal_state(page, "cheque_selection_tab_no_activa")
        return 0, 0

    # "Reference Contains" (misma clase que REFERENCE_SELECTOR, reusada acá con otro
    # propósito) acota la búsqueda a solo las facturas que nosotros cargamos — ver
    # docstring de esta función. Sin esto, SELECT ALL trae también historial ajeno.
    sil.locator(REFERENCE_SELECTOR).fill(supplier_code)
    log.info("    Reference Contains (filtro) = %s", supplier_code)

    # PAYMENT DATE TO: por defecto viene = fecha del invoice y acota la búsqueda a 0.
    # Se setea al último día del mes siguiente para que SEARCH traiga los invoices
    # (solo el filtro; la fecha del cheque queda intacta).
    payment_date_to = search_until_date or _last_day_of_next_month(payment_due_date)
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
        return 0, 0

    expect(select_all_btn).to_be_enabled(timeout=MODAL_TIMEOUT)
    # Asentar la grilla antes de seleccionar: si se clickea SELECT ALL apenas Found>0,
    # a veces la selección no registra y OK nunca se habilita (race observado: 1GIAG1
    # con 172 invoices alcanzaba a asentarse, 1ALT05 con menos no). _select_all_and_wait_ok
    # reintenta hasta que OK quede habilitado.
    page.wait_for_timeout(800)

    # NO se tilda a mano una tanda de N checkboxes: confirmado en vivo (2026-09-30,
    # 1ATRA1) que esta grilla es de selección ÚNICA por fila (tildar una fila desmarca
    # la anterior — el contador "Selected" nunca pasa de 1, sin importar cuántas filas
    # se clickeen). SELECT ALL es el único mecanismo real de selección múltiple; lo que
    # antes se atribuía a "timeout de guardado por escala" (1ING01/1EURO1/1AER01) era en
    # realidad el error de REFERENCE duplicada ya manejado más arriba (ver
    # ReferenceExistsError) — SELECT ALL ya viene probado con 1000+ invoices sin
    # problema (1AER01/USD: 1094, confirmado en una corrida anterior).
    if not _select_all_and_wait_ok(page):
        log.warning("    OK no se habilitó tras SELECT ALL (selección no registró) — abortando")
        _dump_modal_state(page, "cheque_ok_no_habilita")
        return 0, loaded
    found = _read_found_count(page) or loaded
    log.info("    SELECT ALL: %s invoices (FOUND)", found)

    # force=True: con selecciones grandes (cientos de invoices) puede quedar otro
    # <dialog open> del pool de Angular interceptando pointer events sobre este botón
    # (confirmado en vivo: "dialog ... intercepts pointer events"), igual que ya se
    # maneja en el resto del código para clicks entre modales anidados.
    sil.get_by_role("button", name="OK").click(force=True)
    sil.wait_for(state="hidden", timeout=MODAL_TIMEOUT)
    page.wait_for_timeout(800)
    return found, loaded


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
    tener que reproducir el flujo en producción a mano.

    Además del último dialog (buttons/inputs), lista TODOS los <dialog open> con su
    texto y visibilidad real — el diagnóstico original solo miraba botones/inputs del
    último y nunca mostraba el TEXTO, así que un dialog inesperado (error del servidor,
    otro distinto a "Select Invoice Lines") quedaba invisible en los logs."""
    from config.settings import SCREENSHOT_DIR
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.png"))
        info = page.evaluate(
            "() => {"
            " const dialogs = Array.from(document.querySelectorAll('dialog[open]'));"
            " const all = dialogs.map((d, i) => {"
            "   const r = d.getBoundingClientRect();"
            "   const st = window.getComputedStyle(d);"
            "   const visible = r.width > 0 && r.height > 0"
            "     && st.visibility !== 'hidden' && st.display !== 'none';"
            "   return { i, visible, text: (d.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300) };"
            " });"
            " const m = dialogs.length ? dialogs[dialogs.length - 1] : null;"
            " const buttons = m ? Array.from(m.querySelectorAll('button')).map(b => ({"
            "   cls: b.className, txt: (b.textContent || '').trim().slice(0, 30),"
            "   disabled: b.disabled})) : [];"
            " const inputs = m ? Array.from(m.querySelectorAll('input')).map(i => ({"
            "   cls: i.className, ph: i.placeholder || '', val: i.value || ''})) : [];"
            " return { count: dialogs.length, all, buttons, inputs };"
            "}"
        )
        log.warning("    [diag] Modal '%s': %d dialog(s) abiertos", name, info.get("count", 0))
        for d in info.get("all", []):
            log.warning("    [diag]   dialog[%d] visible=%s texto=%r",
                        d["i"], d["visible"], d["text"])
        log.warning("    [diag] Modal '%s' botones (último): %s", name, info.get("buttons"))
        log.warning("    [diag] Modal '%s' inputs (último): %s", name, info.get("inputs"))
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
    #    También se chequea "Transaction error" en cada vuelta (ver
    #    _check_and_dismiss_transaction_error): confirmado en vivo que TourplanNX puede
    #    tirar "Reference Exists" justo acá, no solo al abrir Select Invoice Lines — sin
    #    este chequeo, save_cheque esperaba los CHEQUE_SAVE_TIMEOUT_MS completos (10min)
    #    para nada, reportando un timeout genérico que escondía la causa real.
    waited = 0
    step = 1000
    while waited < CHEQUE_SAVE_TIMEOUT_MS:
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
        error_text = _check_and_dismiss_transaction_error(page)
        if error_text:
            if "Reference Exists" in error_text:
                raise ReferenceExistsError(error_text)
            raise RuntimeError(f"TourplanNX rechazó el guardado del cheque: {error_text}")
        page.wait_for_timeout(step)
        waited += step
    raise RuntimeError("El modal de cheque sigue abierto tras SAVE (timeout)")


def create_cheque(page: Page, supplier_code: str, currency: str, reference: str,
                  cheque_total: float, payment_due_date: str, payment_type: str,
                  search_until_date: str | None = None) -> int:
    """Crea un cheque completo para un proveedor+moneda.

    INSERT → tab CHEQUE → header (con CHEQUE TOTAL) → OK → Select Invoice Lines
    (SELECT ALL) → OK → SAVE. Ante cualquier error aborta los modales y propaga.

    Si TourplanNX rechaza la REFERENCE por ya existir (Error 1038 "Reference Exists" —
    confirmado en vivo 2026-09-30: pasa cuando una corrida anterior ya generó esa misma
    referencia aunque no haya quedado marcada 'ok' en el tracker), reintenta con una
    referencia distinta (sufijo "-R{n}") hasta 3 veces antes de rendirse.

    NOTA sobre selecciones grandes: se probó tildar checkboxes a mano de a tandas para
    evitar seleccionar 1000+ invoices de una — resultó que esta grilla es de selección
    ÚNICA por fila (tildar una desmarca la anterior, confirmado en vivo con 1ATRA1:
    "Selected" nunca pasaba de 1 sin importar cuántas filas se clickearan), así que esa
    idea no es viable. Se usa siempre SELECT ALL (ver confirm_and_select_invoices), que
    ya viene probado con 1000+ invoices sin problema (1AER01/USD: 1094 aplicados).

    search_until_date: ver confirm_and_select_invoices — debe cubrir la factura más
        nueva de la moneda cuando hay invoices de varios meses.

    Returns:
        Cantidad de invoices aplicados (FOUND).
    """
    log.info("  Creando cheque %s (%s, ref=%s, total=%.2f, due=%s)...",
             supplier_code, currency, reference, cheque_total, payment_due_date)
    # El reintento de referencia envuelve el CICLO COMPLETO (no solo la búsqueda): el
    # error "Reference Exists" puede aparecer tanto al abrir Select Invoice Lines como
    # recién en el SAVE final (ver _check_and_dismiss_transaction_error) — confirmado en
    # vivo (2026-09-30, 1CATP1). Si solo se reintentara la búsqueda, un choque detectado
    # en save_cheque perdía todo lo ya encontrado/seleccionado sin poder reintentar.
    for ref_attempt in range(4):
        try_ref = reference if ref_attempt == 0 else f"{reference}-R{ref_attempt}"
        try:
            open_cheque_form(page)
            fill_cheque_header(page, try_ref, currency, cheque_total, payment_due_date, payment_type)
            found, loaded = confirm_and_select_invoices(
                page, payment_due_date, supplier_code, search_until_date)

            if found <= 0:
                log.warning("    Cheque %s/%s sin invoices (FOUND=0) — abortando",
                            supplier_code, currency)
                abort_transaction(page)
                return 0
            save_cheque(page)
            log.info("  Cheque %s/%s guardado: %d invoices aplicados",
                     supplier_code, currency, found)
            return found
        except ReferenceExistsError:
            log.warning("    Referencia %s ya existe en TourplanNX — reintentando con otra",
                        try_ref)
            try:
                abort_transaction(page)
            except Exception:
                pass
        except Exception:
            try:
                abort_transaction(page)
            except Exception:
                pass
            raise

    log.warning("    Cheque %s/%s: no se encontró una referencia libre tras varios "
                "intentos — abortando", supplier_code, currency)
    return 0
