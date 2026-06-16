import argparse
import os
import sys
import threading
from pathlib import Path
from threading import Event

from core.browser import BrowserManager
from core.grouping import export_grouped_csv
from core.session import SessionStore
from core.stats import StatsTracker, StatsEventHandler
from core.pipeline import run_pipeline, get_sheet_names, INPUT_DIR
from data.tracker import ProcessTracker
from modules.login import do_login, is_logged_in
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


def _cleanup(stats: StatsTracker, browser: BrowserManager, error: str | None = None):
    if error:
        stats.error = error
        stats.finished = True
        try:
            browser.screenshot("error_fatal")
        except Exception:
            pass
    else:
        stats.finished = True
    try:
        stats.save_report("report")
    except Exception:
        pass


def _close_browser(browser: BrowserManager):
    log.info("Cerrando navegador...")
    try:
        browser.close()
    except Exception:
        pass
    log.info("Navegador cerrado.")


def _force_exit(stats: StatsTracker, code: int | None = None):
    if code is None:
        code = 1 if stats.error else 0
    log.info("Saliendo con código %d", code)
    for h in log.handlers:
        h.flush()
    import logging
    logging.shutdown()
    os._exit(code)


def _finish(browser: BrowserManager, stats: StatsTracker, error: str | None = None):
    _cleanup(stats, browser, error)
    t = threading.Thread(target=_close_browser, args=(browser,), daemon=True)
    t.start()
    t.join(timeout=15)
    if threading.current_thread() is threading.main_thread():
        _force_exit(stats)


def run_automation(
    stats: StatsTracker,
    headless: bool = False,
    test_config: dict | None = None,
    no_tracker: bool = False,
    use_session: bool = True,
    limit: int | None = None,
    max_vouchers: int | None = None,
    _browser: BrowserManager | None = None,
    _stop_event: Event | None = None,
):
    stats.start_run()
    browser = _browser if _browser is not None else BrowserManager(headless=headless)
    stop_event = _stop_event if _stop_event is not None else _make_stop()
    tracker = ProcessTracker() if not no_tracker else None
    store = SessionStore()

    try:
        log.info("Iniciando automatización (headless=%s)", headless)

        if use_session and store.exists():
            log.info("Restaurando sesión guardada...")
            page = browser.start(storage_state=store.state_path(), init_script=store.init_script())
        else:
            page = browser.start()

        log.info("Navegando a creditor...")
        page.goto(spa_url("creditor"))
        page.wait_for_load_state("networkidle")

        if not is_logged_in(page):
            log.info("Sesión expirada o no existe — haciendo login...")
            do_login(page, stats)
            page.goto(spa_url("creditor"))
            page.wait_for_load_state("networkidle")
        else:
            log.info("Sesión activa — sin re-login.")

        if use_session:
            browser.save_session(store)
            log.info("Sesión guardada en disco.")

        run_pipeline(page, stats, tracker, stop_event=stop_event, test_config=test_config, no_tracker=no_tracker, limit=limit, max_vouchers=max_vouchers)

        log.info("Automatización completada. Progreso: %s%%", stats.progress)

    except KeyboardInterrupt:
        log.info("Interrupción por teclado")
        _finish(browser, stats, "Interrumpido por el usuario")
        return

    except Exception as e:
        log.error("Error fatal: %s", e)
        _finish(browser, stats, str(e))
        return

    _finish(browser, stats)


def run_pipeline_only(
    stats: StatsTracker,
    headless: bool = False,
    test_config: dict | None = None,
    no_tracker: bool = False,
    use_session: bool = True,
    _browser: BrowserManager | None = None,
    _stop_event: Event | None = None,
):
    stats.start_run()
    browser = _browser if _browser is not None else BrowserManager(headless=headless)
    stop_event = _stop_event if _stop_event is not None else _make_stop()
    tracker = ProcessTracker() if not no_tracker else None
    store = SessionStore()

    try:
        log.info("Iniciando pipeline solo (headless=%s)", headless)

        if use_session and store.exists():
            log.info("Restaurando sesión guardada...")
            page = browser.start(storage_state=store.state_path(), init_script=store.init_script())
        else:
            page = browser.start()

        log.info("Navegando a creditor...")
        page.goto(spa_url("creditor"))
        page.wait_for_load_state("networkidle")

        if not is_logged_in(page):
            log.info("Sesión expirada o no existe — haciendo login...")
            do_login(page, stats)
            page.goto(spa_url("creditor"))
            page.wait_for_load_state("networkidle")
        else:
            log.info("Sesión activa — sin re-login.")

        if use_session:
            browser.save_session(store)
            log.info("Sesión guardada en disco.")

        run_pipeline(page, stats, tracker, stop_event=stop_event, test_config=test_config, no_tracker=no_tracker)

        log.info("Pipeline completado. Progreso: %s%%", stats.progress)

    except KeyboardInterrupt:
        log.info("Interrupción por teclado")
        _finish(browser, stats, "Interrumpido por el usuario")
        return

    except Exception as e:
        log.error("Error fatal: %s", e)
        _finish(browser, stats, str(e))
        return

    _finish(browser, stats)


class RunManager:
    """Dueño del ciclo de vida de cada run: thread, stats, browser y stop_event."""

    HUNG_AFTER = 120.0  # segundos sin heartbeat con thread vivo → estado "hung"

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stats: StatsTracker | None = None
        self._browser: BrowserManager | None = None
        self._run_stop_event: Event | None = None
        self._mode: str | None = None
        log.addHandler(StatsEventHandler(lambda: self._stats))

    @property
    def stats(self) -> StatsTracker | None:
        return self._stats

    def start(
        self,
        mode: str,
        *,
        headless: bool = True,
        test_config: dict | None = None,
        no_tracker: bool = False,
    ) -> tuple[bool, str]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "Ya hay una automatización en ejecución"

            stop_ev = Event()
            stats = StatsTracker()
            browser = BrowserManager(headless=headless)

            target = run_automation if mode == "full" else run_pipeline_only
            t = threading.Thread(
                target=target,
                kwargs={
                    "stats": stats,
                    "headless": headless,
                    "test_config": test_config,
                    "no_tracker": no_tracker,
                    "_browser": browser,
                    "_stop_event": stop_ev,
                },
                daemon=True,
            )

            self._thread = t
            self._stats = stats
            self._browser = browser
            self._run_stop_event = stop_ev
            self._mode = mode

            t.start()
            log.info("RunManager: run '%s' iniciado (headless=%s)", mode, headless)
            return True, f"{'Automatización' if mode == 'full' else 'Pipeline'} iniciado"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False, "No hay ninguna automatización en ejecución"
            if self._run_stop_event:
                self._run_stop_event.set()
            log.info("RunManager: señal de detención enviada")
            return True, "Deteniendo... el pipeline se detendrá al finalizar el grupo actual"

    def force_stop(self) -> tuple[bool, str]:
        """Para forzado: cierra el navegador para desbloquear al worker Playwright.
        BLOQUEANTE (hasta ~15s). Llamar siempre desde un thread plano, nunca desde asyncio."""
        with self._lock:
            browser = self._browser
            stop_ev = self._run_stop_event
            thread = self._thread

        if thread is None or not thread.is_alive():
            return False, "No hay ninguna automatización en ejecución"

        if stop_ev:
            stop_ev.set()

        if browser:
            close_t = threading.Thread(target=browser.force_close, daemon=True)
            close_t.start()
            close_t.join(timeout=10)

        if thread:
            thread.join(timeout=8)

        alive = thread.is_alive() if thread else False
        if alive:
            return False, "El thread sigue vivo tras el cierre forzado — puede necesitar reiniciar uvicorn"
        return True, "Detención forzada completada"

    def state(self) -> str:
        thread = self._thread
        stats = self._stats
        stop_ev = self._run_stop_event

        if thread is None:
            return "idle"

        alive = thread.is_alive()

        if not alive:
            if stats and stats.error:
                return "error"
            return "finished" if stats and stats.finished else "idle"

        if stop_ev and stop_ev.is_set():
            return "stopping"

        if stats:
            age = stats.last_activity_age
            if age is not None and age > self.HUNG_AFTER:
                return "hung"

        return "running"

    def snapshot(self) -> dict:
        thread = self._thread
        stats = self._stats
        return {
            "state": self.state(),
            "mode": self._mode,
            "thread_alive": thread.is_alive() if thread else False,
            "heartbeat_age": stats.last_activity_age if stats else None,
            "stats": stats.results if stats else None,
        }


# Singleton compartido entre main.py y monitor/app.py
run_manager = RunManager()


# ── Wrappers de compatibilidad ────────────────────────────────────────────────

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
    parser.add_argument("--sheet", type=str, default=None, help="Nombre de la hoja (default: auto-detect)")
    parser.add_argument("--no-tracker", action="store_true", help="Desactivar tracker (procesa siempre)")
    parser.add_argument("--tracker", type=str, choices=["status", "reset"], help="Gestión del tracker")
    parser.add_argument("--file", type=str, help="Archivo para --tracker reset")
    parser.add_argument("--all", action="store_true", help="Resetear todo el tracker")
    parser.add_argument("--export-csv", action="store_true", help="Exportar CSV agrupado por proveedor (sin abrir navegador)")
    parser.add_argument("--fresh-login", action="store_true", help="Ignorar sesión guardada y hacer login desde cero")
    parser.add_argument("--clear-session", action="store_true", help="Borrar la sesión guardada en disco y salir")
    parser.add_argument("--supplier", type=str, help="Procesar solo el proveedor con este Supplier_Code (para testing masivo)")
    parser.add_argument("--limit", type=int, default=None, help="Procesar como máximo N proveedores pendientes por corrida (modo lote)")
    parser.add_argument("--max-vouchers", type=int, default=None, help="Saltar proveedores con más de N vouchers (default: settings; <=0 sin límite)")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.clear_session:
        SessionStore().clear()
        print("Sesión borrada.")
        sys.exit(0)

    if args.export_csv:
        filepath = Path(args.file) if args.file else next(iter(sorted(INPUT_DIR.glob("*.xlsx"))), None)
        if not filepath or not filepath.exists():
            print("No se encontró archivo .xlsx. Usá --file <ruta>.")
            sys.exit(1)
        detail, summary = export_grouped_csv(filepath, args.sheet)
        print(f"Detalle:  {detail}")
        print(f"Resumen:  {summary}")
        sys.exit(0)

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
    if args.visible:
        headless = False

    test_config = None
    if args.test:
        test_config = {"test": True}
        if args.sheet is not None:
            test_config["sheet"] = args.sheet
        if args.row is not None:
            test_config["row"] = args.row
        if args.supplier is not None:
            test_config["supplier"] = args.supplier

    use_session = not args.fresh_login
    if args.fresh_login:
        SessionStore().clear()
        log.info("--fresh-login: sesión anterior borrada.")

    stats = StatsTracker()
    run_automation(stats, headless=headless, test_config=test_config, no_tracker=args.no_tracker, use_session=use_session, limit=args.limit, max_vouchers=args.max_vouchers)

    sys.exit(1 if stats.error else 0)


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
