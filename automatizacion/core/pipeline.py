import csv
import re
import shutil
from pathlib import Path
from threading import Event

import pandas as pd

from config.settings import BASE_DIR, MAX_VOUCHERS_PER_SUPPLIER
from config.urls import spa_url
from core.grouping import group_rows_by_supplier, write_skipped_report, write_oversized_report
from data.tracker import ProcessTracker
from modules.creditor_search import open_supplier
from modules.login import ensure_logged_in
from modules.supplier_nav import navigate_to_transactions, exit_supplier
from modules.transaction_creator import (
    create_transaction,
    create_bulk_transaction,
    confirm_bulk_transaction,
    add_vouchers_via_search,
    read_invoice_totals,
    save_invoice,
    abort_transaction,
    VoucherSearchTimeout,
)
from utils.logger import log


INPUT_DIR = BASE_DIR / "input"
PROCESSED_DIR = BASE_DIR / "processed"


class PipelineStopped(Exception):
    """Excepción interna para señal de stop cooperativo dentro de un grupo."""
    pass


_MESES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_FECHA_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?: \d{2}:\d{2}:\d{2}(?:\.\d+)?)?$")

# Overrides verificados manualmente contra TourplanNX: códigos cuyo valor reconstruido
# canónico (DMONYY) NO coincide con el código real guardado en el sistema.
# Confirmados buscándolos en el creditor search (2026-06-18).
_OVERRIDES_CODIGO = {
    "1APR01": "1APRI1",   # Valeria Marina Aprile (Guía)
    "6JUL02": "6JUL2",    # Julie Clugnac (Guía)
    "6AUG01": "6AUGU1",   # Augusto Ushuaia (Restaurante)
}


def _reconstruir_valor_fecha(value):
    """Si Excel convirtió un código/voucher a fecha, reconstruye 'DMONYY' (ej: 1MAR02).

    Devuelve (valor_final, fue_reconstruido). Solo toca strings con pinta de fecha
    stringificada por pandas; los códigos/vouchers normales pasan sin cambios.
    Aplica overrides verificados para los códigos cuyo formato real difiere del canónico.
    """
    if not isinstance(value, str):
        return value, False
    m = _FECHA_RE.match(value.strip())
    if not m:
        return value, False
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    canonico = f"{d}{_MESES[mo - 1]}{y % 100:02d}"
    return _OVERRIDES_CODIGO.get(canonico, canonico), True


# Columnas donde Excel puede auto-convertir un valor tipo "1MAR02" a fecha.
_COLUMNAS_FECHA = ("Supplier_Code", "Voucher_Number")


def _escribir_reporte_reconstruidos(filepath: Path, reconstruidos: list[tuple]):
    """Escribe un CSV con los valores que se reconstruyeron desde fecha (transparencia)."""
    out_dir = BASE_DIR / "outputs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"fecha_reconstruidos_{filepath.stem}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row_index", "columna", "valor_leido_excel", "valor_reconstruido"])
        w.writerows(reconstruidos)
    log.info("Reporte de valores reconstruidos desde fecha: %s (%d entradas)", out_path, len(reconstruidos))


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

    records = data.to_dict(orient="records")
    reconstruidos = []
    for i, rec in enumerate(records):
        for col in _COLUMNAS_FECHA:
            if col not in rec:
                continue
            nuevo, cambio = _reconstruir_valor_fecha(rec.get(col))
            if cambio:
                reconstruidos.append((i, col, rec.get(col), nuevo))
                rec[col] = nuevo
    if reconstruidos:
        log.warning("get_data_rows: %d valor(es) reconstruidos desde fecha (ej: %s %s -> %s)",
                    len(reconstruidos), reconstruidos[0][1], reconstruidos[0][2], reconstruidos[0][3])
        _escribir_reporte_reconstruidos(filepath, reconstruidos)
    return records


TARGET_SHEET = "TODO SERVICIOS SIN TRANS"


def get_sheet_names(filepath: Path) -> list[str]:
    xls = pd.ExcelFile(filepath)
    sheets = xls.sheet_names
    if TARGET_SHEET in sheets:
        return [TARGET_SHEET]
    return [s for s in sheets if s not in ("Sheet2",)]


def process_row(page, row: dict, row_index: int, filename: str, tracker: ProcessTracker | None, stats, stop_event: Event | None = None):
    """Procesa un único registro. Se usa cuando el proveedor tiene solo 1 voucher."""
    supplier_code = (row.get("Supplier_Code") or "").strip()
    voucher = row.get("Voucher_Number", "?")

    if stop_event and stop_event.is_set():
        raise PipelineStopped()

    log.info("  Procesando fila %d: %s (proveedor: %s)", row_index, voucher, supplier_code)
    stats.set_activity(supplier=supplier_code, currency=row.get("Service_Cost_Currency", ""), voucher=voucher, voucher_idx=1, voucher_total=1)

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
        stats.add_voucher_result({
            "filename": filename, "supplier_code": supplier_code,
            "voucher": str(voucher), "currency": row.get("Service_Cost_Currency", ""),
            "status": "ok", "error": None, "row_index": row_index,
        })
        log.info("  Fila %d OK: %s", row_index, voucher)
    except Exception as e:
        if tracker:
            tracker.mark_row_failed(filename, row_index, str(e))
        stats.add_voucher_result({
            "filename": filename, "supplier_code": supplier_code,
            "voucher": str(voucher), "currency": row.get("Service_Cost_Currency", ""),
            "status": "failed", "error": str(e), "row_index": row_index,
        })
        log.error("  Fila %d FAILED: %s — %s", row_index, voucher, e)


def process_supplier_group(
    page,
    group: dict,
    rows: list[dict],
    filename: str,
    tracker: ProcessTracker | None,
    stats,
    skipped_report: list | None = None,
    stop_event: Event | None = None,
    max_vouchers: int | None = None,
    oversized_report: list | None = None,
):
    """Procesa todos los vouchers de un proveedor en un solo ciclo de navegación."""
    supplier_code = group["supplier_code"]
    records = group["records"]

    # Marcar las filas MEP como 'skipped'
    skipped_mep = group.get("skipped_mep", [])
    if skipped_mep:
        log.info("  Proveedor %s — %d registros MEP ignorados (filas: %s)",
                 supplier_code, len(skipped_mep), skipped_mep)
        if tracker:
            tracker.mark_rows_skipped_bulk(filename, skipped_mep)

    # Proveedores grandes: superan el umbral de vouchers → saltar y reportar
    # (entidades internas tipo 1EURO1/1ING01, inviables de cargar por UI).
    if max_vouchers and max_vouchers > 0 and group["size"] > max_vouchers:
        currencies = ", ".join(group["total_by_currency"].keys())
        totals = "; ".join(f"{c}={t:.2f}" for c, t in group["total_by_currency"].items())
        log.warning("  Proveedor %s SALTADO: %d vouchers > umbral %d — para revisión manual",
                    supplier_code, group["size"], max_vouchers)
        if tracker:
            tracker.mark_rows_skipped_bulk(filename, [rec["row_index"] for rec in records])
        if oversized_report is not None:
            oversized_report.append({
                "filename": filename,
                "supplier_code": supplier_code,
                "supplier_name": group.get("supplier_name", ""),
                "voucher_count": group["size"],
                "currencies": currencies,
                "totals": totals,
            })
        return

    if group["size"] == 1:
        rec = records[0]
        process_row(page, rows[rec["row_index"]], rec["row_index"], filename, tracker, stats, stop_event=stop_event)
        return

    if not group["total_by_currency"]:
        log.warning("  Proveedor %s — todos los registros son MEP, nada que procesar", supplier_code)
        return

    log.info("  Proveedor %s — %d registros, masivo por moneda: %s",
             supplier_code, group["size"],
             ", ".join(f"{c}={t:.2f}" for c, t in group["total_by_currency"].items()))

    stats.set_activity(supplier=supplier_code, supplier_name=group.get("supplier_name", ""))

    all_indices = [rec["row_index"] for rec in records]

    for idx in all_indices:
        if tracker:
            tracker.mark_row_processing(filename, idx)

    group_ok = True
    group_error = None
    group_timeout = False
    per_row_status: dict[int, str] = {}

    try:
        open_supplier(page, supplier_code)
        navigate_to_transactions(page)

        for currency, total in group["total_by_currency"].items():
            # Chequear stop antes de abrir INSERT (no hay modal abierto)
            if stop_event and stop_event.is_set():
                try:
                    exit_supplier(page)
                except Exception:
                    pass
                raise PipelineStopped()

            records_for_currency = [rec for rec in records if rec["currency"] == currency]
            vouchers_for_currency = [rec["voucher"] for rec in records_for_currency]
            reference = f"INV{records_for_currency[0]['row_index']}{supplier_code}"
            log.info("    Moneda %s: total=%.2f, ref=%s, %d vouchers",
                     currency, total, reference, len(records_for_currency))

            stats.set_activity(currency=currency, voucher_total=len(records_for_currency), voucher_idx=0)

            page.get_by_role("button", name="INSERT").click()
            create_bulk_transaction(page, total, currency, reference)
            confirm_bulk_transaction(page)

            # Calcular rango VOUCHER FROM/TO para acotar la búsqueda en el servidor
            try:
                nums = [int(v.replace(",", "")) for v in vouchers_for_currency
                        if str(v).replace(",", "").isdigit()]
                vfrom = str(min(nums)) if nums else None
                vto = str(max(nums)) if nums else None
            except Exception:
                vfrom = vto = None

            # Carga masiva vía modal "Select Vouchers" (lupa)
            try:
                result = add_vouchers_via_search(page, vouchers_for_currency, vfrom, vto)
            except VoucherSearchTimeout:
                log.warning("    VoucherSearchTimeout %s/%s → oversized", supplier_code, currency)
                try:
                    abort_transaction(page)
                except Exception:
                    pass
                if oversized_report is not None:
                    oversized_report.append({
                        "filename": filename,
                        "supplier_code": supplier_code,
                        "supplier_name": group.get("supplier_name", ""),
                        "voucher_count": group["size"],
                        "currencies": ", ".join(group["total_by_currency"].keys()),
                        "totals": "; ".join(f"{c}={t:.2f}" for c, t in group["total_by_currency"].items()),
                    })
                group_ok = False
                group_timeout = True
                group_error = f"VoucherSearchTimeout ({currency})"
                break

            # Registrar resultado por fila
            not_found_set = {str(v).replace(",", "") for v in result["not_found"]}
            for rec in records_for_currency:
                norm = str(rec["voucher"]).replace(",", "")
                if norm in not_found_set:
                    entry = {
                        "filename": filename,
                        "supplier_code": supplier_code,
                        "supplier_name": group.get("supplier_name", ""),
                        "currency": currency,
                        "voucher": rec["voucher"],
                        "account": "no_encontrado",
                        "reason": "Voucher no encontrado en TourplanNX (lupa)",
                        "row_index": rec["row_index"],
                    }
                    if skipped_report is not None:
                        skipped_report.append(entry)
                    stats.add_skipped(entry)
                    stats.add_voucher_result({
                        "filename": filename, "supplier_code": supplier_code,
                        "voucher": rec["voucher"], "currency": currency,
                        "status": "skipped", "error": entry["reason"], "row_index": rec["row_index"],
                    })
                    per_row_status[rec["row_index"]] = "skipped"
                else:
                    stats.add_voucher_result({
                        "filename": filename, "supplier_code": supplier_code,
                        "voucher": rec["voucher"], "currency": currency,
                        "status": "ok", "error": None, "row_index": rec["row_index"],
                    })
                    per_row_status[rec["row_index"]] = "ok"

            loaded_count = len(result["loaded"])

            if loaded_count == 0:
                log.warning("    Ningún voucher cargado para %s/%s — abortando", supplier_code, currency)
                abort_transaction(page)
                group_ok = False
                group_error = f"Ningún voucher encontrado en lupa ({currency})"
            else:
                if result["not_found"]:
                    log.warning("    %d/%d vouchers no encontrados en lupa: %s",
                                len(result["not_found"]), len(vouchers_for_currency), result["not_found"])

                totals = read_invoice_totals(page)
                remainder = abs(totals.get("remainder", 9999))

                if remainder < 0.01:
                    log.info("    Totales cuadran: %s %s — REMAINDER=%.2f", supplier_code, currency, remainder)
                else:
                    log.warning("    REMAINDER=%.4f para %s %s — guardando igual",
                                totals.get("remainder", "?"), supplier_code, currency)

                save_invoice(page)

        exit_supplier(page)

    except PipelineStopped:
        raise
    except Exception as e:
        log.error("  Proveedor %s FAILED: %s", supplier_code, e)
        group_ok = False
        group_error = str(e)
        try:
            abort_transaction(page)
        except Exception:
            pass
        try:
            exit_supplier(page)
        except Exception:
            pass
        # Hard recovery: forzar navegación a creditor y verificar sesión
        try:
            page.goto(spa_url("creditor"))
            page.wait_for_load_state("networkidle", timeout=15000)
            ensure_logged_in(page, stats)
        except Exception:
            pass

    if group_timeout:
        if tracker:
            tracker.mark_rows_skipped_bulk(filename, all_indices)
        log.warning("  Proveedor %s marcado oversized por timeout (%d filas skipped)",
                    supplier_code, len(all_indices))
    elif group_ok:
        for idx in all_indices:
            status = per_row_status.get(idx, "ok")
            if tracker:
                if status == "skipped":
                    tracker.mark_row_skipped(filename, idx)
                else:
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
    limit: int | None = None,
    max_vouchers: int | None = None,
):
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    skipped_report: list[dict] = []
    oversized_report: list[dict] = []

    # Umbral de proveedores grandes (None → usar default de settings; <=0 → sin límite)
    effective_max = max_vouchers if max_vouchers is not None else MAX_VOUCHERS_PER_SUPPLIER

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
        stats.set_activity(file=filename, sheet=sheet_name)

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
            # Recuperar grupos interrumpidos por una caída previa (processing → pending)
            recovered = tracker.reset_processing_to_pending(filename)
            if recovered:
                log.info("  Recuperadas %d filas 'processing' → 'pending' (corrida previa interrumpida)", recovered)
            pending_rows = tracker.get_pending_rows(filename)
        else:
            pending_rows = [{"row_index": i} for i in range(len(rows))]

        if test_config and test_config.get("test"):
            supplier_filter = test_config.get("supplier")
            suppliers_filter = test_config.get("suppliers")
            row_filter = test_config.get("row")
            if suppliers_filter:
                pending_rows = [
                    r for r in pending_rows
                    if (rows[r["row_index"]].get("Supplier_Code") or "").strip() in suppliers_filter
                ]
                if not pending_rows:
                    log.warning("  Ningún proveedor de %s encontrado o ya procesado", suppliers_filter)
            elif supplier_filter:
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

        if limit is not None and limit > 0 and len(groups) > limit:
            log.info("  Lote: procesando %d de %d proveedores pendientes", limit, len(groups))
            groups = groups[:limit]

        pipeline_stopped = False
        for i, group in enumerate(groups, 1):
            if stop_event and stop_event.is_set():
                log.info("Detenido por usuario durante el procesamiento")
                pipeline_stopped = True
                break

            label = f"Proveedor {group['supplier_code']} ({group['size']} voucher{'s' if group['size'] > 1 else ''})"
            step = stats.add_step(label)
            stats.mark_running(step)
            try:
                process_supplier_group(page, group, rows, filename, tracker, stats, skipped_report,
                                       stop_event=stop_event, max_vouchers=effective_max,
                                       oversized_report=oversized_report)
                stats.mark_ok(step)
            except PipelineStopped:
                stats.mark_skipped(step)
                log.info("  Detenido durante proveedor %s — filas vuelven a pending", group["supplier_code"])
                if tracker:
                    for rec in group["records"]:
                        tracker.mark_row_pending(filename, rec["row_index"])
                pipeline_stopped = True
                break
            except Exception as e:
                stats.mark_failed(step, str(e))

            log.info("  Progreso archivo: %d/%d proveedores", i, len(groups))

        # Determinar si mover el archivo o dejarlo en input/
        remaining = tracker.get_pending_rows(filename) if tracker else []
        stopped = pipeline_stopped or bool(stop_event and stop_event.is_set())

        if remaining or stopped:
            n = len(remaining)
            if tracker:
                tracker.mark_file_processing(filename)
            log.info("Archivo %s incompleto: quedan %d filas pendientes — permanece en input/", filename, n)
        else:
            moved = PROCESSED_DIR / filename
            shutil.move(str(filepath), str(moved))
            log.info("Archivo movido a processed/: %s", filename)
            if tracker:
                tracker.mark_file_completed(filename)

        if pipeline_stopped:
            break

        if test_config and test_config.get("test"):
            log.info("Modo prueba: solo 1 archivo procesado")
            break

    report_path = write_skipped_report(skipped_report)
    if report_path:
        log.info("Reporte de vouchers salteados: %s (%d entradas)", report_path, len(skipped_report))
    oversized_path = write_oversized_report(oversized_report)
    if oversized_path:
        log.info("Reporte de proveedores grandes (saltados): %s (%d entradas)", oversized_path, len(oversized_report))
    log.info("Pipeline finalizado")
