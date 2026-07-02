"""Orquestación de la creación de cheques por proveedor+moneda.

Fuente de trabajo: los vouchers en estado `ok` del tracker (invoices ya cargados por
el pipeline principal), agrupados por (supplier_code, currency). Por cada grupo se
emite UN cheque que aplica todos los invoices de esa moneda (SELECT ALL).

La fecha del PAYMENT DUE DATE se lee de la grilla de Transactions en vivo (no del
tracker, que no la guarda). La REFERENCE es OP{row_index_más_bajo}{supplier_code}.

Idempotencia: `processed_cheques` registra cada cheque; los que ya están `ok` se saltan.
"""
from config.settings import CHEQUE_REFERENCE_PREFIX, CHEQUE_PAYMENT_TYPE, CHEQUE_EXEMPT_FILE
from config.urls import spa_url
from core.exceptions import SupplierNotFoundError
from data.tracker import ProcessTracker
from modules.creditor_search import open_supplier
from modules.login import ensure_logged_in
from modules.supplier_nav import navigate_to_transactions, exit_supplier
from modules.transaction_creator import abort_transaction
from checks.cheque_creator import create_cheque, read_invoice_summary_by_currency
from checks.voucher_filter import get_ok_vouchers
from utils.logger import log


def _load_exempt_suppliers() -> set[str]:
    """Lee proveedores_exentos.csv → set de Supplier_Code (uppercase). Vacío si no existe."""
    try:
        text = CHEQUE_EXEMPT_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return set()
    return {ln.strip().upper() for ln in text.splitlines() if ln.strip()}


def _plan_from_tracker(tracker: ProcessTracker) -> dict:
    """Agrupa los vouchers ok por proveedor → {supplier_code: {currency: min_row_index}}."""
    plan: dict[str, dict[str, int]] = {}
    for r in get_ok_vouchers(tracker=tracker):
        sup = r.get("supplier_code")
        cur = (r.get("currency") or "").strip()
        ri = r.get("row_index")
        if not sup or not cur or ri is None:
            continue
        cur_map = plan.setdefault(sup, {})
        if cur not in cur_map or ri < cur_map[cur]:
            cur_map[cur] = ri
    return plan


def run_cheque_pipeline(page, stats, tracker: ProcessTracker | None = None,
                        stop_event=None, test_config: dict | None = None) -> None:
    """Recorre proveedores con vouchers ok y emite un cheque por moneda.

    En modo test (`--supplier`/`--suppliers`) sin datos en el tracker, descubre las
    monedas leyendo la grilla de Transactions; la REFERENCE usa row_index=0.

    Si se pasa `stop_event`, se chequea entre proveedores (stop cooperativo): no
    interrumpe el cheque en curso, corta el loop limpio en la siguiente iteración.
    """
    test_config = test_config or {}
    supplier_filter = test_config.get("supplier")
    suppliers_filter = test_config.get("suppliers")

    plan: dict[str, dict[str, int]] = {}
    if tracker:
        plan = _plan_from_tracker(tracker)

    if suppliers_filter:
        plan = {s: c for s, c in plan.items() if s in suppliers_filter}
        for s in suppliers_filter:
            plan.setdefault(s, {})   # forzar aunque no esté en el tracker
    elif supplier_filter:
        plan = {s: c for s, c in plan.items() if s == supplier_filter}
        plan.setdefault(supplier_filter, {})

    exempt = _load_exempt_suppliers()
    if exempt:
        skipped = [s for s in plan if s.upper() in exempt]
        for s in skipped:
            log.info("Proveedor %s exento — skip cheques (proveedores_exentos.csv)", s)
            plan.pop(s)
        if skipped:
            log.info("Exentos salteados: %d proveedor(es)", len(skipped))

    if not plan:
        log.info("No hay proveedores con vouchers ok para emitir cheques")
        return

    log.info("Cheques a procesar: %d proveedor(es)", len(plan))

    for supplier_code, currency_rows in plan.items():
        if stop_event is not None and stop_event.is_set():
            log.info("Detenido por usuario — corte entre proveedores")
            break
        stats.set_activity(supplier=supplier_code)
        try:
            open_supplier(page, supplier_code)
            navigate_to_transactions(page)

            summary = read_invoice_summary_by_currency(page)
            # Monedas objetivo: las del tracker; si no hay (modo test), las de la grilla.
            target_currencies = list(currency_rows.keys()) if currency_rows else list(summary.keys())

            for i, currency in enumerate(target_currencies):
                if currency not in summary:
                    log.warning("  %s: moneda %s sin invoices en la grilla — skip",
                                supplier_code, currency)
                    continue
                if tracker and tracker.is_cheque_done(supplier_code, currency):
                    log.info("  %s/%s ya tiene cheque ok — skip (idempotencia)",
                             supplier_code, currency)
                    continue

                # row_index para la REFERENCE: el más bajo de la moneda (tracker) o, en
                # modo grilla sin tracker, el índice de enumeración (único por moneda
                # para que ARS y USD no colisionen en OP{row_index}{code}).
                row_index = currency_rows.get(currency, i)
                reference = f"{CHEQUE_REFERENCE_PREFIX}{row_index}{supplier_code}"
                due = summary[currency]["date"]
                total = summary[currency]["total"]
                try:
                    found = create_cheque(page, supplier_code, currency, reference, total, due,
                                          CHEQUE_PAYMENT_TYPE)
                    if tracker:
                        if found and found > 0:
                            tracker.mark_cheque_ok(supplier_code, currency, reference, due)
                        else:
                            # create_cheque devolvió 0 (modal sin invoices, abortado): NO es
                            # éxito — marcarlo failed para que no quede falsamente 'ok' y la
                            # idempotencia lo reintente.
                            tracker.mark_cheque_failed(
                                supplier_code, currency, reference,
                                "Select Invoice Lines sin invoices (FOUND=0)", due)
                except Exception as e:
                    log.error("  Cheque %s/%s FAILED: %s", supplier_code, currency, e)
                    if tracker:
                        tracker.mark_cheque_failed(supplier_code, currency, reference, str(e), due)

            exit_supplier(page)

        except SupplierNotFoundError as e:
            log.warning("  Proveedor %s no encontrado en TourplanNX — skip cheques (%s)",
                        supplier_code, e)
        except Exception as e:
            log.error("  Proveedor %s FAILED en cheques: %s", supplier_code, e)
            # Hard recovery: cerrar modales, salir y recargar la SPA.
            try:
                abort_transaction(page)
            except Exception:
                pass
            try:
                exit_supplier(page)
            except Exception:
                pass
            try:
                page.goto(spa_url("creditor"))
                page.reload(wait_until="networkidle")
                page.wait_for_load_state("networkidle", timeout=15000)
                ensure_logged_in(page, stats)
            except Exception:
                pass

    log.info("Pipeline de cheques finalizado")
