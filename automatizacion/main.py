import argparse
import sys
import threading
from threading import Event

from core.browser import BrowserManager
from core.stats import StatsTracker
from core.pipeline import run_pipeline, get_sheet_names
from data.tracker import ProcessTracker
from modules.login import do_login, ensure_logged_in
from config.urls import spa_url
from utils.logger import log

_stop_event: Event | None = None


def _make_stop():
    global _stop_event
    if _stop_event is None:
        _stop_event = Event()
    return _stop_event


def stop_automation():
    e = _make_stop()
    e.set()
    log.info("Señal de detención enviada")


def reset_stop():
    global _stop_event
    if _stop_event:
        _stop_event.clear()


def run_automation(stats: StatsTracker, headless: bool = False, test_config: dict | None = None):
    stats.start_run()
    browser = BrowserManager(headless=headless)
    tracker = ProcessTracker()

    try:
        log.info("Iniciando automatización (headless=%s)", headless)
        page = browser.start()

        do_login(page, stats)

        log.info("Navegando a creditor...")
        page.goto(spa_url("creditor"))
        page.wait_for_load_state("networkidle")

        run_pipeline(page, stats, tracker, stop_event=_stop_event, test_config=test_config)

        stats.finished = True
        stats.save_report("report")
        log.info("Automatización completada. Progreso: %s%%", stats.progress)

    except Exception as e:
        log.error("Error fatal: %s", e)
        stats.error = str(e)
        stats.finished = True
        if hasattr(browser, "screenshot"):
            browser.screenshot("error_fatal")
        stats.save_report("report")
    finally:
        browser.close()


def run_pipeline_only(stats: StatsTracker, headless: bool = False, test_config: dict | None = None):
    stats.start_run()
    browser = BrowserManager(headless=headless)
    tracker = ProcessTracker()

    try:
        log.info("Iniciando pipeline solo (headless=%s)", headless)
        page = browser.start()

        ensure_logged_in(page, stats)

        log.info("Navegando a creditor...")
        page.goto(spa_url("creditor"))
        page.wait_for_load_state("networkidle")

        run_pipeline(page, stats, tracker, stop_event=_stop_event, test_config=test_config)

        stats.finished = True
        stats.save_report("report")
        log.info("Pipeline completado. Progreso: %s%%", stats.progress)

    except Exception as e:
        log.error("Error fatal: %s", e)
        stats.error = str(e)
        stats.finished = True
        if hasattr(browser, "screenshot"):
            browser.screenshot("error_fatal")
        stats.save_report("report")
    finally:
        browser.close()


def run_automation_thread(stats: StatsTracker, headless: bool = False, test_config: dict | None = None):
    reset_stop()
    t = threading.Thread(target=run_automation, args=(stats, headless), kwargs={"test_config": test_config}, daemon=True)
    t.start()
    return t


def run_pipeline_thread(stats: StatsTracker, headless: bool = False, test_config: dict | None = None):
    reset_stop()
    t = threading.Thread(target=run_pipeline_only, args=(stats, headless), kwargs={"test_config": test_config}, daemon=True)
    t.start()
    return t


def parse_args():
    parser = argparse.ArgumentParser(description="Automatización de tareas web")
    parser.add_argument("--visible", action="store_true", help="Abrir navegador visible")
    parser.add_argument("--headless", action="store_true", default=False, help="Ejecutar en segundo plano")
    parser.add_argument("--report", type=str, default="report", help="Nombre del archivo de reporte")
    parser.add_argument("--test", action="store_true", help="Modo prueba: solo 1 registro")
    parser.add_argument("--row", type=int, help="Fila específica a procesar (0-indexed)")
    parser.add_argument("--sheet", type=str, default="SI TRANS", help="Nombre de la hoja (default: SI TRANS)")
    parser.add_argument("--tracker", type=str, choices=["status", "reset"], help="Gestión del tracker")
    parser.add_argument("--file", type=str, help="Archivo para --tracker reset")
    parser.add_argument("--all", action="store_true", help="Resetear todo el tracker")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.tracker:
        tracker = ProcessTracker()
        if args.tracker == "status":
            _show_status(tracker)
        elif args.tracker == "reset":
            if args.all:
                tracker.reset_all()
                print("Tracker reseteado completamente.")
            elif args.file:
                tracker.reset_file(args.file)
                print(f"Tracker reseteado para: {args.file}")
            else:
                print("Usá --file <nombre> o --all")
        return

    headless = args.headless
    if not headless and not args.visible:
        headless = True

    test_config = None
    if args.test:
        test_config = {"test": True, "sheet": args.sheet}
        if args.row is not None:
            test_config["row"] = args.row

    stats = StatsTracker()
    run_automation(stats, headless=headless, test_config=test_config)

    if stats.error:
        sys.exit(1)


def _show_status(tracker: ProcessTracker):
    summary = tracker.get_summary()
    if not summary:
        print("No hay archivos procesados aún.")
        return
    print(f"{'Archivo':<40} {'Status':<12} {'Total':<8} {'OK':<8} {'Failed':<8}")
    print("-" * 80)
    for s in summary:
        print(f"{s['filename']:<40} {s['status']:<12} {s['total_rows']:<8} {s['ok_rows']:<8} {s['failed_rows']:<8}")
    print()
    from core.pipeline import INPUT_DIR
    pending_files = [f for f in sorted(INPUT_DIR.glob("*.xlsx"))]
    if pending_files:
        print(f"Archivos pendientes en input/: {len(pending_files)}")
        for f in pending_files:
            print(f"  - {f.name}")


if __name__ == "__main__":
    main()
