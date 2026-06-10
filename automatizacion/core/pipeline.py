import shutil
from pathlib import Path
from threading import Event

import pandas as pd

from config.settings import BASE_DIR
from data.tracker import ProcessTracker
from modules.creditor_search import open_supplier
from modules.supplier_nav import navigate_to_transactions, exit_supplier
from modules.transaction_creator import create_transaction
from utils.logger import log


INPUT_DIR = BASE_DIR / "input"
PROCESSED_DIR = BASE_DIR / "processed"


def get_data_rows(filepath: Path, sheet_name: str | None = None) -> list[dict]:
    if sheet_name is None:
        sheets = get_sheet_names(filepath)
        if not sheets:
            return []
        sheet_name = sheets[0]
    df = pd.read_excel(filepath, sheet_name=sheet_name, dtype=str, header=None)
    if len(df) < 4:
        return []
    headers = df.iloc[2].tolist()
    data = df.iloc[3:].copy()
    data.columns = headers
    data = data.dropna(axis=1, how="all")
    data = data.loc[:, data.columns.notna()]
    data = data.reset_index(drop=True)
    return data.to_dict(orient="records")


def get_sheet_names(filepath: Path) -> list[str]:
    xls = pd.ExcelFile(filepath)
    return [s for s in xls.sheet_names if s not in ("Sheet2",)]


def process_row(page, row: dict, row_index: int, filename: str, tracker: ProcessTracker | None, stats):
    supplier_code = (row.get("Supplier_Code") or "").strip()
    voucher = row.get("Voucher_Number", "?")
    log.info("  Procesando fila %d: %s (proveedor: %s)", row_index, voucher, supplier_code)

    if tracker:
        tracker.mark_row_processing(filename, row_index)

    try:
        open_supplier(page, supplier_code)
        navigate_to_transactions(page)
        page.get_by_role("button", name="INSERT").click()
        create_transaction(page, row, row_index)
        page.get_by_role("dialog").get_by_role("button", name="EXIT").click()
        page.get_by_role("dialog").wait_for(state="hidden", timeout=5000)
        exit_supplier(page)
        if tracker:
            tracker.mark_row_ok(filename, row_index)
        log.info("  Fila %d OK: %s", row_index, voucher)
    except Exception as e:
        if tracker:
            tracker.mark_row_failed(filename, row_index, str(e))
        log.error("  Fila %d FAILED: %s — %s", row_index, voucher, e)


def run_pipeline(
    page,
    stats,
    tracker: ProcessTracker | None = None,
    stop_event: Event | None = None,
    test_config: dict | None = None,
    no_tracker: bool = False,
):
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    if no_tracker:
        tracker = None
        pending_files = sorted(INPUT_DIR.glob("*.xlsx"))
    else:
        if tracker is None:
            tracker = ProcessTracker()
        pending_files = tracker.find_pending_files(INPUT_DIR)

    if not pending_files:
        log.info("No hay archivos pendientes en input/")
        return

    log.info("Archivos pendientes: %d", len(pending_files))

    for filepath in pending_files:
        if stop_event and stop_event.is_set():
            log.info("Detenido por usuario")
            break

        filename = filepath.name
        if test_config and test_config.get("sheet"):
            sheet_name = test_config["sheet"]
        else:
            sheets = get_sheet_names(filepath)
            sheet_name = sheets[0] if sheets else None
        if not sheet_name:
            log.warning("  Sin hojas en %s, moviendo a processed/", filename)
            shutil.move(str(filepath), str(PROCESSED_DIR / filename))
            continue
        log.info("Procesando archivo: %s (hoja: %s)", filename, sheet_name)

        rows = get_data_rows(filepath, sheet_name)
        if not rows:
            log.warning("  Sin datos en %s, moviendo a processed/", sheet_name)
            shutil.move(str(filepath), str(PROCESSED_DIR / filename))
            continue

        if tracker:
            file_hash = tracker.file_hash(filepath)
            tracker.mark_file_pending(filename, file_hash, len(rows))
            tracker.init_rows(filename, rows)
            tracker.mark_file_processing(filename)
            pending_rows = tracker.get_pending_rows(filename)
        else:
            pending_rows = [{"row_index": i} for i in range(len(rows))]

        if test_config and test_config.get("test"):
            row_filter = test_config.get("row")
            if row_filter is not None:
                pending_rows = [r for r in pending_rows if r["row_index"] == row_filter]
                if not pending_rows:
                    log.warning("  Fila %d ya procesada o no existe", row_filter)
            else:
                pending_rows = pending_rows[:1]

        total = len(pending_rows)

        for i, db_row in enumerate(pending_rows, 1):
            if stop_event and stop_event.is_set():
                log.info("Detenido por usuario durante el procesamiento")
                break

            row_index = db_row["row_index"]
            data_row = rows[row_index]
            step = stats.add_step(f"Fila {row_index}: {data_row.get('Voucher_Number', '?')}")
            stats.mark_running(step)
            try:
                process_row(page, data_row, row_index, filename, tracker, stats)
                stats.mark_ok(step)
            except Exception as e:
                stats.mark_failed(step, str(e))

            log.info("  Progreso archivo: %d/%d", i, total)

        moved = PROCESSED_DIR / filename
        shutil.move(str(filepath), str(moved))
        log.info("Archivo movido a processed/: %s", filename)

        if tracker:
            if stop_event and stop_event.is_set():
                tracker.mark_file_completed(filename, "Detenido por usuario")
            else:
                tracker.mark_file_completed(filename)

        if test_config and test_config.get("test"):
            log.info("Modo prueba: solo 1 archivo procesado")
            break

    log.info("Pipeline finalizado")
