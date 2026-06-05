import argparse
import sys
import threading

from core.browser import BrowserManager
from core.stats import StatsTracker
from config.settings import ENV
from utils.logger import log


def run_automation(stats: StatsTracker, headless: bool = False):
    stats.start_run()
    browser = BrowserManager(headless=headless)

    try:
        log.info("Iniciando automatización (headless=%s)", headless)
        page = browser.start()

        # --- Punto de entrada para los módulos ---
        # Ejemplo:
        # from modules.login import do_login
        # do_login(page, stats, url=ENV["URL"], username=ENV["USERNAME"], password=ENV["PASSWORD"])

        stats.finished = True
        stats.save_report("report")
        log.info("Automatización completada. Progreso: %s%%", stats.progress)
        log.info("Resultados: %s", stats.results)

    except Exception as e:
        log.error("Error fatal: %s", e)
        stats.error = str(e)
        stats.finished = True
        if hasattr(browser, "screenshot"):
            browser.screenshot("error_fatal")
        stats.save_report("report")
    finally:
        browser.close()


def run_automation_thread(stats: StatsTracker, headless: bool = False):
    t = threading.Thread(target=run_automation, args=(stats, headless), daemon=True)
    t.start()
    return t


def parse_args():
    parser = argparse.ArgumentParser(description="Automatización de tareas web")
    parser.add_argument("--visible", action="store_true", help="Abrir navegador visible")
    parser.add_argument("--headless", action="store_true", default=False, help="Ejecutar en segundo plano")
    parser.add_argument("--report", type=str, default="report", help="Nombre del archivo de reporte")
    return parser.parse_args()


def main():
    args = parse_args()
    headless = args.headless
    if not headless and not args.visible:
        headless = True

    stats = StatsTracker()
    run_automation(stats, headless=headless)

    if stats.error:
        sys.exit(1)


if __name__ == "__main__":
    main()
