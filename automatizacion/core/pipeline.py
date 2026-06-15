import shutil
from pathlib import Path
from threading import Event

import pandas as pd

from config.settings import BASE_DIR
from core.grouping import group_rows_by_supplier, write_skipped_report
from data.tracker import ProcessTracker
from modules.creditor_search import open_supplier
from modules.supplier_nav import navigate_to_transactions, exit_supplier
from modules.transaction_creator import (
    create_transaction,
    create_bulk_transaction,
    confirm_bulk_transaction,
    add_voucher_line,
    exit_invoice_line,
    read_invoice_totals,
    save_invoice,
    abort_transaction,
    InvalidAccountError,
)
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
    if len(df) < 3:
        return []
    # Detectar la fila de headers buscando "Supplier_Code" en las primeras 10 filas
    header_row = 2
    for i in range(min(10, len(df))):
        if "Supplier_Code" in [str(v) for v in df.iloc[i].tolist()]:
            header_row = i
            break
    if len(df) <= header_row + 1:
        return []
    headers = df.iloc[header_row].tolist()
    data = df.iloc[header_row + 1:].copy()
    data.columns = headers
    data = data.dropna(axis=1, how="all")
    data = data.loc[:, data.columns.notna()]
    data = data.reset_index(drop=True)
    return data.to_dict(orient="records")


TARGET_SHEET = "TODO SERVICIOS SIN TRANS"


def get_sheet_names(filepath: Path) -> list[str]:
    xls = pd.ExcelFile(filepath)
    sheets = xls.sheet_names
    if TARGET_SHEET in sheets:
        return [TARGET_SHEET]
    return [s for s in sheets if s not in ("Sheet2",)]


def process_row(page, row: dict, row_index: int, filename: str, tracker: ProcessTracker | None, stats):
    """Procesa un único registro. Se usa cuando el proveedor tiene solo 1 voucher."""
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


def process_supplier_group(page, group: dict, rows: list[dict], filename: str, tracker: ProcessTracker | None, stats, skipped_report: list | None = None):
    """Procesa todos los vouchers de un proveedor en un solo ciclo de navegación.

    Si el grupo tiene un solo registro delega en process_row (comportamiento original).
    Si tiene varios, abre el proveedor una vez y repite INSERT→llenar→EXIT modal
    por cada voucher sin salir del proveedor entre ellos.
    """
    supplier_code = group["supplier_code"]
    records = group["records"]

    if group["size"] == 1:
        rec = records[0]
        process_row(page, rows[rec["row_index"]], rec["row_index"], filename, tracker, stats)
        return

    skipped_mep = group.get("skipped_mep", [])
    if skipped_mep:
        log.info("  Proveedor %s — %d registros MEP ignorados (filas: %s)",
                 supplier_code, len(skipped_mep), skipped_mep)

    if not group["total_by_currency"]:
        log.warning("  Proveedor %s — todos los registros son MEP, nada que procesar", supplier_code)
        return

    log.info("  Proveedor %s — %d registros, masivo por moneda: %s",
             supplier_code, group["size"],
             ", ".join(f"{c}={t:.2f}" for c, t in group["total_by_currency"].items()))

    all_indices = [rec["row_index"] for rec in records]

    for idx in all_indices:
        if tracker:
            tracker.mark_row_processing(filename, idx)

    group_ok = True
    group_error = None

    try:
        open_supplier(page, supplier_code)
        navigate_to_transactions(page)

        for currency, total in group["total_by_currency"].items():
            records_for_currency = [rec for rec in records if rec["currency"] == currency]
            vouchers_for_currency = [rec["voucher"] for rec in records_for_currency]
            reference = f"INV{records_for_currency[0]['row_index']}{supplier_code}"
            log.info("    Moneda %s: total=%.2f, ref=%s, %d vouchers: %s",
                     currency, total, reference, len(records_for_currency), vouchers_for_currency)

            page.get_by_role("button", name="INSERT").click()
            create_bulk_transaction(page, total, currency, reference)
            confirm_bulk_transaction(page)

            skipped_vouchers: list[str] = []
            for i, rec in enumerate(records_for_currency):
                voucher = rec["voucher"]
                try:
                    add_voucher_line(page, voucher, is_first=(i == 0))
                except InvalidAccountError as e:
                    log.warning("    Voucher %s saltado — cuenta inválida: %s", voucher, e)
                    exit_invoice_line(page)
                    skipped_vouchers.append(voucher)
                    if skipped_report is not None:
                        skipped_report.append({
                            "filename": filename,
                            "supplier_code": supplier_code,
                            "supplier_name": group.get("supplier_name", ""),
                            "currency": currency,
                            "voucher": e.voucher or voucher,
                            "account": e.account or "?",
                            "reason": str(e),
                            "row_index": rec["row_index"],
                        })

            loaded_count = len(vouchers_for_currency) - len(skipped_vouchers)

            if loaded_count == 0:
                log.warning("    Todos los vouchers de %s/%s saltados — abortando", supplier_code, currency)
                abort_transaction(page)
                group_ok = False
                group_error = f"Todos los vouchers con cuenta inválida ({currency})"
            else:
                totals = read_invoice_totals(page)
                remainder = abs(totals.get("remainder", 9999))

                if skipped_vouchers:
                    log.warning("    %d/%d vouchers cargados (saltados por cuenta inválida: %s)",
                                loaded_count, len(vouchers_for_currency), skipped_vouchers)

                # Política: guardar siempre que haya al menos 1 voucher cargado,
                # sin importar el REMAINDER (TourplanNX lo permite). La diferencia
                # queda registrada en el invoice para revisión posterior.
                if remainder < 0.01:
                    log.info("    Totales cuadran: %s %s — REMAINDER=%.2f", supplier_code, currency, remainder)
                else:
                    log.warning("    REMAINDER=%.4f para %s %s — guardando igual (discrepancia/salteados)",
                                totals.get("remainder", "?"), supplier_code, currency)

                save_invoice(page)

        exit_supplier(page)

    except Exception as e:
        log.error("  Proveedor %s FAILED: %s", supplier_code, e)
        group_ok = False
        group_error = str(e)
        try:
            abort_transaction(page)
        except Exception:
            pass

    if group_ok:
        for idx in all_indices:
            if tracker:
                tracker.mark_row_ok(filename, idx)
        log.info("  Proveedor %s masivo completado (%d filas)", supplier_code, len(all_indices))
    else:
        for idx in all_indices:
            if tracker:
                tracker.mark_row_failed(filename, idx, group_error or "masivo fallido")
        log.error("  Proveedor %s masivo FAILED: %s", supplier_code, group_error)


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
    skipped_report: list[dict] = []

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
            supplier_filter = test_config.get("supplier")
            row_filter = test_config.get("row")
            if supplier_filter:
                pending_rows = [
                    r for r in pending_rows
                    if (rows[r["row_index"]].get("Supplier_Code") or "").strip() == supplier_filter
                ]
                if not pending_rows:
                    log.warning("  Proveedor %s no encontrado o ya procesado", supplier_filter)
            elif row_filter is not None:
                pending_rows = [r for r in pending_rows if r["row_index"] == row_filter]
                if not pending_rows:
                    log.warning("  Fila %d ya procesada o no existe", row_filter)
            else:
                pending_rows = pending_rows[:1]

        total = len(pending_rows)
        pending_indices = [r["row_index"] for r in pending_rows]
        groups = group_rows_by_supplier(rows, pending_indices)

        for i, group in enumerate(groups, 1):
            if stop_event and stop_event.is_set():
                log.info("Detenido por usuario durante el procesamiento")
                break

            label = f"Proveedor {group['supplier_code']} ({group['size']} voucher{'s' if group['size'] > 1 else ''})"
            step = stats.add_step(label)
            stats.mark_running(step)
            try:
                process_supplier_group(page, group, rows, filename, tracker, stats, skipped_report)
                stats.mark_ok(step)
            except Exception as e:
                stats.mark_failed(step, str(e))

            log.info("  Progreso archivo: %d/%d proveedores", i, len(groups))

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

    report_path = write_skipped_report(skipped_report)
    if report_path:
        log.info("Reporte de vouchers salteados: %s (%d entradas)", report_path, len(skipped_report))
    log.info("Pipeline finalizado")
