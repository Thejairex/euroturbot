"""CLI del módulo de chequeos / creación de cheques.

Subcomandos (cwd = automatizacion/):

  # Consultar el filtro de vouchers por estatus (read-only):
  python -m checks.main filter                       # vouchers OK de todos los archivos
  python -m checks.main filter --status failed       # otro estatus
  python -m checks.main filter --file "archivo.xlsx" # acotar a un archivo
  python -m checks.main filter --count               # solo contar

  # Crear cheques (orden de pago) a partir de los invoices ya cargados:
  python -m checks.main run                          # todos los proveedores con vouchers ok
  python -m checks.main run --supplier 6IRE1 --no-tracker --visible
  python -m checks.main run --suppliers 6IRE1,1USH03

  # Backfill de invoice_reference en cheques ya registrados (OP{idx}{code} -> INV{idx}{code}):
  python -m checks.main backfill-invoice-ref --dry-run   # solo cuenta, no modifica
  python -m checks.main backfill-invoice-ref             # aplica y reporta filas actualizadas

Sin subcomando se asume `filter` (compatibilidad).
"""
import argparse

from checks.voucher_filter import get_vouchers_by_status
from data.tracker import ProcessTracker


def cmd_filter(args) -> None:
    tracker = ProcessTracker()
    try:
        if args.count:
            n = tracker.count_rows_by_status(args.status, args.file)
            print(f"Vouchers con estatus '{args.status}': {n}")
            return
        vouchers = get_vouchers_by_status(args.status, args.file, tracker=tracker)
        print(f"Vouchers con estatus '{args.status}': {len(vouchers)}\n")
        for v in vouchers:
            print(
                f"  [{v['row_index']:>5}] voucher={v['voucher_number']!s:>12}  "
                f"proveedor={v['supplier_code']!s:<10}  moneda={v['currency']!s:<4}  "
                f"({v['filename']})"
            )
    finally:
        tracker.close()


def cmd_backfill_invoice_ref(args) -> None:
    tracker = ProcessTracker()
    try:
        pending = tracker.count_cheques_missing_invoice_ref()
        if args.dry_run:
            print(f"Cheques a actualizar (dry-run, sin modificar): {pending}")
            return
        updated = tracker.backfill_invoice_references()
        print(f"invoice_reference actualizado en {updated} cheque(s).")
    finally:
        tracker.close()


def cmd_run(args) -> None:
    # Imports diferidos: solo el subcomando run necesita navegador/login.
    from core.browser import BrowserManager
    from core.session import SessionStore
    from core.stats import StatsTracker
    from config.urls import spa_url
    from modules.login import do_login, is_logged_in
    from checks.cheque_pipeline import run_cheque_pipeline
    from utils.logger import log

    headless = not args.visible
    test_config = {}
    if args.supplier:
        test_config["supplier"] = args.supplier
    if args.suppliers:
        test_config["suppliers"] = [s.strip() for s in args.suppliers.split(",") if s.strip()]

    stats = StatsTracker()
    stats.start_run()
    browser = BrowserManager(headless=headless)
    tracker = None if args.no_tracker else ProcessTracker()
    store = SessionStore()

    try:
        log.info("Iniciando creación de cheques (headless=%s)", headless)
        if store.exists():
            page = browser.start(storage_state=store.state_path(), init_script=store.init_script())
        else:
            page = browser.start()

        page.goto(spa_url("creditor"))
        page.wait_for_load_state("networkidle")
        if not is_logged_in(page):
            log.info("Sesión expirada — login...")
            do_login(page, stats)
            page.goto(spa_url("creditor"))
            page.wait_for_load_state("networkidle")
        else:
            log.info("Sesión activa — sin re-login.")
        browser.save_session(store)

        run_cheque_pipeline(page, stats, tracker, test_config=test_config)
        log.info("Creación de cheques completada.")
    finally:
        try:
            browser.close()
        except Exception:
            pass
        if tracker:
            tracker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Chequeos y creación de cheques")
    sub = parser.add_subparsers(dest="command")

    p_filter = sub.add_parser("filter", help="Consultar vouchers por estatus (read-only)")
    p_filter.add_argument("--status", default="ok",
                          help="Estatus a filtrar (ok/failed/skipped/pending/processing)")
    p_filter.add_argument("--file", default=None, help="Acotar a un archivo")
    p_filter.add_argument("--count", action="store_true", help="Solo mostrar el total")

    p_run = sub.add_parser("run", help="Crear cheques desde los invoices cargados")
    p_run.add_argument("--supplier", default=None, help="Procesar solo este Supplier_Code")
    p_run.add_argument("--suppliers", default=None, help="Supplier_Codes separados por coma")
    p_run.add_argument("--no-tracker", action="store_true", dest="no_tracker",
                       help="No usar tracker (test directo; requiere --supplier)")
    p_run.add_argument("--visible", action="store_true", help="Navegador visible")

    p_backfill = sub.add_parser("backfill-invoice-ref",
                                help="Rellenar invoice_reference en cheques ya registrados")
    p_backfill.add_argument("--dry-run", action="store_true",
                            help="Solo contar filas a actualizar, sin modificar")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "backfill-invoice-ref":
        cmd_backfill_invoice_ref(args)
    else:
        # default: filter (compatibilidad si no se pasa subcomando)
        if args.command is None:
            args.status, args.file, args.count = "ok", None, False
        cmd_filter(args)


if __name__ == "__main__":
    main()
