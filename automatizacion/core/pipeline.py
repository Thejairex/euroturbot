import shutil
import time
from pathlib import Path
from threading import Event

import pandas as pd

from config.settings import BASE_DIR
from data.tracker import ProcessTracker
from utils.logger import log


INPUT_DIR = BASE_DIR / "input"
PROCESSED_DIR = BASE_DIR / "processed"


def get_data_rows(filepath: Path, sheet_name: str = "SI TRANS") -> list[dict]:
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


def process_row(page, row: dict, row_index: int, filename: str, tracker: ProcessTracker, stats):
    ref = row.get("Booking_Reference", "?")
    log.info("  Procesando fila %d: %s", row_index, ref)

    tracker.mark_row_processing(filename, row_index)

    try:
        # ── Aquí se insertará la lógica de subida al formulario ──
        time.sleep(1)

        tracker.mark_row_ok(filename, row_index)
        log.info("  Fila %d OK: %s", row_index, ref)
    except Exception as e:
        tracker.mark_row_failed(filename, row_index, str(e))
        log.error("  Fila %d FAILED: %s — %s", row_index, ref, e)


def run_pipeline(
    page,
    stats,
    tracker: ProcessTracker | None = None,
    stop_event: Event | None = None,
    test_config: dict | None = None,
):
    if tracker is None:
        tracker = ProcessTracker()

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

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
        sheet_name = test_config.get("sheet", "SI TRANS") if test_config else "SI TRANS"
        log.info("Procesando archivo: %s (hoja: %s)", filename, sheet_name)

        rows = get_data_rows(filepath, sheet_name)
        if not rows:
            log.warning("  Sin datos en %s, moviendo a processed/", sheet_name)
            shutil.move(str(filepath), str(PROCESSED_DIR / filename))
            continue

        file_hash = tracker.file_hash(filepath)
        tracker.mark_file_pending(filename, file_hash, len(rows))
        tracker.init_rows(filename, rows)
        tracker.mark_file_processing(filename)

        pending_rows = tracker.get_pending_rows(filename)
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
            step = stats.add_step(f"Fila {row_index}: {data_row.get('Booking_Reference', '?')}")
            stats.mark_running(step)
            try:
                process_row(page, data_row, row_index, filename, tracker, stats)
                stats.mark_ok(step)
            except Exception as e:
                stats.mark_failed(step, str(e))
                tracker.mark_row_failed(filename, row_index, str(e))

            log.info("  Progreso archivo: %d/%d", i, total)

        if stop_event and stop_event.is_set():
            tracker.mark_file_completed(filename, "Detenido por usuario")
        else:
            tracker.mark_file_completed(filename)
            dest = PROCESSED_DIR / filename
            shutil.move(str(filepath), str(dest))
            log.info("Archivo movido a processed/: %s", filename)

        if test_config and test_config.get("test"):
            log.info("Modo prueba: solo 1 archivo procesado")
            break

    log.info("Pipeline finalizado")
