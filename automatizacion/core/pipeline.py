import csv
import re
import shutil
from pathlib import Path
from threading import Event

import pandas as pd

from config.settings import (
    BASE_DIR,
    MAX_VOUCHERS_PER_SUPPLIER,
    MAX_VOUCHERS_DEFER_THRESHOLD,
    READ_EXISTING_REFS,
    VOUCHER_CHUNK_SIZE,
    VOUCHER_MAX_RANGE_WIDTH,
)
from config.urls import spa_url
from core.exceptions import SupplierNotFoundError
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
    ReferenceExistsError,
)
from utils.logger import log


INPUT_DIR = BASE_DIR / "input"
PROCESSED_DIR = BASE_DIR / "processed"


class PipelineStopped(Exception):
    """Excepción interna para señal de stop cooperativo dentro de un grupo."""
    pass


def _voucher_int(voucher) -> int:
    """Número de voucher como int para ordenar/calcular rangos (no-dígitos → 0)."""
    s = str(voucher).replace(",", "").strip()
    return int(s) if s.isdigit() else 0


def _bloque_desde(sub: list[dict]) -> dict:
    """Arma el dict de un bloque/chunk a partir de sus records."""
    nums = [_voucher_int(r.get("voucher")) for r in sub if _voucher_int(r.get("voucher"))]
    return {
        "records": sub,
        "vouchers": [r.get("voucher") for r in sub],
        "total": sum(float(r.get("product_cost") or 0) for r in sub),
        "vfrom": str(min(nums)) if nums else None,
        "vto": str(max(nums)) if nums else None,
    }


def chunk_records_for_invoice(
    records: list[dict], chunk_size: int, max_range_width: int = 0
) -> list[dict]:
    """Divide los records de una moneda en bloques (cada uno = una factura), ordenados
    por número de voucher.

    Un bloque se corta cuando se cumple lo PRIMERO de:
      - juntar `chunk_size` vouchers, o
      - que el ancho del rango (voucher_actual - voucher_inicio_del_bloque) supere
        `max_range_width` (evita el timeout SQL del SEARCH por rango muy ancho).

    Un proveedor chico (<= chunk_size y rango angosto) resulta en 1 solo bloque →
    comportamiento idéntico al flujo masivo de una sola factura.

    Returns:
        Lista de dicts {records, vouchers, total, vfrom, vto} por bloque.
    """
    if chunk_size <= 0:
        chunk_size = len(records) or 1
    ordenados = sorted(records, key=lambda r: _voucher_int(r.get("voucher")))
    bloques: list[dict] = []
    actual: list[dict] = []
    inicio = None
    for rec in ordenados:
        n = _voucher_int(rec.get("voucher"))
        if actual:
            excede_cant = len(actual) >= chunk_size
            excede_ancho = max_range_width > 0 and inicio is not None and n - inicio > max_range_width
            if excede_cant or excede_ancho:
                bloques.append(_bloque_desde(actual))
                actual = []
                inicio = None
        if not actual:
            inicio = n if n else None
        actual.append(rec)
    if actual:
        bloques.append(_bloque_desde(actual))
    return bloques


def _classify_by_loaded(
    records: list[dict], loaded_vouchers: set[str]
) -> tuple[list[int], list[int]]:
    """Clasifica row_indices según si su voucher está en loaded_vouchers.

    Si loaded_vouchers está vacío (el campo de voucher no se pudo leer de la API),
    se tratan todos como ok — fallback seguro.

    Returns:
        (ok_indices, skipped_indices)
    """
    if not loaded_vouchers:
        return [r["row_index"] for r in records], []
    ok, skipped = [], []
    for r in records:
        vnorm = str(r.get("voucher", "")).replace(",", "").strip()
        if vnorm in loaded_vouchers:
            ok.append(r["row_index"])
        else:
            skipped.append(r["row_index"])
    return ok, skipped


def _read_existing_references(page, supplier_code: str) -> dict[str, set[str]]:
    """Consulta la API GetAccountingTransactions para obtener referencias INV* existentes
    y los vouchers que contiene cada una.

    Devuelve {reference: {voucher_str, ...}}. Si no se puede leer el campo de voucher
    para una referencia, su set queda vacío — el caller debe tratar eso como "todos ok"
    (fallback seguro: no marcar nada como skipped sin evidencia).

    Devuelve {} si la API falla o no hay transacciones previas.
    """
    if not READ_EXISTING_REFS:
        # Desactivado: la consulta cuelga en proveedores con historial grande (ver
        # settings). La dedup la cubre el manejo de 1038 'Reference Exists' por factura.
        return {}
    try:
        # La consulta GetAccountingTransactions (ShowTrans:'All' desde 2019) puede ser
        # enorme para proveedores masivos (ej. 1ING01, 52k+ registros) y colgar el
        # page.evaluate indefinidamente (no tiene timeout). Se acota con Promise.race +
        # AbortController: si tarda > TIMEOUT, devuelve {__timeout:true} y se sigue sin
        # dedup (el 1038 'Reference Exists' se maneja por chunk/factura igual).
        raw = page.evaluate("""
            async (code) => {
                const TIMEOUT_MS = 90000;
                const ctrl = new AbortController();
                const timeout = new Promise((resolve) => setTimeout(
                    () => { ctrl.abort(); resolve({__timeout: true}); }, TIMEOUT_MS));
                const work = (async () => {
                    let resp;
                    try {
                        resp = await fetch(
                            '/tourplannx/tourplanservices/Services/Accounting.svc/json/GetAccountingTransactions',
                            {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                                body: JSON.stringify({request: {
                                    Code: code,
                                    Ledger: 'AccountsPayable',
                                    InCurrency: null,
                                    Branch: null,
                                    OrderBy: 'TranDate',
                                    ShowTrans: 'All',
                                    DateFrom: '2019-10-01T00:00:00'
                                }}),
                                signal: ctrl.signal
                            }
                        );
                    } catch (e) { return {__error: String(e)}; }
                    if (!resp.ok) return {};
                    const data = await resp.json();
                    const lines = data.AccountingTransactionLines || [];
                    const firstInv = lines.find(l => l.TransactionReference && l.TransactionReference.startsWith('INV'));
                    const fieldKeys = firstInv ? Object.keys(firstInv) : [];
                    const result = {};
                    for (const l of lines) {
                        if (!l.TransactionReference || !l.TransactionReference.startsWith('INV')) continue;
                        const ref = l.TransactionReference;
                        if (!result[ref]) result[ref] = [];
                        const vno = l.VoucherNo || l.VoucherNumber || l.Voucher || l.VoucherRef || '';
                        const vstr = String(vno).replace(/,/g, '').trim();
                        if (vstr && vstr !== '0' && vstr !== 'null' && vstr !== 'undefined') {
                            result[ref].push(vstr);
                        }
                    }
                    return {refs: result, fieldKeys};
                })();
                return await Promise.race([work, timeout]);
            }
        """, supplier_code)
        raw = raw or {}
        if raw.get("__timeout"):
            log.warning("  [DEBUG] _read_existing_references: timeout (90s) para %s — proveedor con "
                        "historial enorme; se sigue sin dedup (1038 se maneja por factura)", supplier_code)
            return {}
        if raw.get("__error"):
            log.warning("  [DEBUG] _read_existing_references fetch error %s: %s", supplier_code, raw.get("__error"))
            return {}
        field_keys = raw.get("fieldKeys", [])
        refs_raw = raw.get("refs", {})
        result: dict[str, set[str]] = {ref: set(vs) for ref, vs in refs_raw.items()}
        inv_refs = [r for r in result if r.startswith("INV")]
        log.info("  [DEBUG] Referencias INV* en TourplanNX (%d): %s", len(inv_refs), sorted(inv_refs))
        if field_keys:
            log.info("  [DEBUG] Campos API línea: %s", field_keys)
        # Advertir si no se encontraron vouchers en ninguna referencia (campo desconocido)
        refs_sin_vouchers = [r for r in result if not result[r]]
        if refs_sin_vouchers and len(refs_sin_vouchers) == len(result):
            log.warning("  [DEBUG] No se leyeron vouchers de la API (campo desconocido) — se usará fallback ok")
        return result
    except Exception as exc:
        log.warning("  [DEBUG] _read_existing_references falló: %s", exc)
        return {}


def _cargar_chunk_factura(
    page, supplier_code, currency, chunk_records, select_all, piso=25,
    tracker=None, filename=None,
):
    """Carga un chunk de records como UNA factura (INSERT → create_bulk → confirm →
    lupa SELECT ALL → SAVE).

    Actualiza el tracker inmediatamente al terminar cada chunk (ok o failed), de modo
    que un reinicio solo reintenta los chunks que realmente fallaron sin duplicar los
    que ya se guardaron en TourplanNX.

    Si el SEARCH da timeout (VoucherSearchTimeout), aborta y sub-divide el chunk por
    rango (mitades) reintentando cada mitad como su propia factura, hasta un piso de
    `piso` vouchers. Los chunks irrecuperables se devuelven como fallidos.

    Returns:
        (ok_indices, failed_indices) — row_index de las filas cargadas / fallidas.
    """
    indices = [r["row_index"] for r in chunk_records]
    vouchers = [r["voucher"] for r in chunk_records]
    nums = [_voucher_int(r["voucher"]) for r in chunk_records if _voucher_int(r["voucher"])]
    vfrom = str(min(nums)) if nums else None
    vto = str(max(nums)) if nums else None
    total = sum(float(r.get("product_cost") or 0) for r in chunk_records)
    ref = f"INV{chunk_records[0]['row_index']}{supplier_code}"

    try:
        page.locator("#creditorview").get_by_role("button", name="INSERT").click()
        create_bulk_transaction(page, total, currency, ref)
        try:
            confirm_bulk_transaction(page)
        except ReferenceExistsError:
            # La referencia ya existe en TourplanNX (no la detectó _read_existing_references).
            # Marcar ok: la factura está guardada.
            log.info("    Chunk %s: 1038 Reference Exists — ya guardado, marcando %d filas ok", ref, len(indices))
            try:
                abort_transaction(page)
            except Exception:
                pass
            if tracker and filename:
                tracker.mark_rows_ok_bulk(filename, indices, reference=ref)
            return (indices, [])
        result = add_vouchers_via_search(page, vouchers, vfrom, vto, select_all=select_all)
        if len(result["loaded"]) == 0:
            log.warning("    Chunk sin vouchers cargados (%s) — abortando", currency)
            abort_transaction(page)
            if tracker and filename:
                tracker.mark_rows_failed_bulk(filename, indices, "Sin vouchers cargados en lupa")
            return ([], indices)
        totals = read_invoice_totals(page)
        rem = abs(totals.get("remainder", 9999))
        if rem >= 0.01:
            log.warning("    REMAINDER=%.2f en chunk %s — guardando igual", totals.get("remainder", 0), currency)
        save_invoice(page)
        log.info("    Factura guardada: %d filas (%s)", len(indices), currency)
        if tracker and filename:
            tracker.mark_rows_ok_bulk(filename, indices, reference=ref)
            log.info("  [DEBUG] Tracker: %d filas → ok (ref=%s)", len(indices), ref)
        return (indices, [])
    except VoucherSearchTimeout:
        try:
            abort_transaction(page)
        except Exception:
            pass
        if len(chunk_records) <= piso:
            log.error("    Chunk mínimo (%d) sigue en timeout — marcando %d filas failed",
                      len(chunk_records), len(indices))
            if tracker and filename:
                tracker.mark_rows_failed_bulk(filename, indices, "VoucherSearchTimeout en chunk mínimo")
            return ([], indices)
        mid = len(chunk_records) // 2
        log.warning("    VoucherSearchTimeout en chunk de %d (%s) — sub-dividiendo en %d + %d",
                    len(chunk_records), currency, mid, len(chunk_records) - mid)
        ok1, f1 = _cargar_chunk_factura(
            page, supplier_code, currency, chunk_records[:mid], select_all, piso, tracker, filename)
        ok2, f2 = _cargar_chunk_factura(
            page, supplier_code, currency, chunk_records[mid:], select_all, piso, tracker, filename)
        return (ok1 + ok2, f1 + f2)
    except Exception as e:
        # Cualquier otro fallo del chunk (SAVE colgado, modal inesperado, etc.): abortar
        # y marcar este chunk failed sin tirar abajo los demás chunks del proveedor.
        log.error("    Chunk falló (%s): %s — marcando %d filas failed", currency, e, len(indices))
        try:
            abort_transaction(page)
        except Exception:
            pass
        if tracker and filename:
            tracker.mark_rows_failed_bulk(filename, indices, str(e))
        return ([], indices)


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
        page.locator("#creditorview").get_by_role("button", name="INSERT").click()
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
    except SupplierNotFoundError as e:
        if tracker:
            tracker.mark_row_skipped(filename, row_index)
        stats.add_voucher_result({
            "filename": filename, "supplier_code": supplier_code,
            "voucher": str(voucher), "currency": row.get("Service_Cost_Currency", ""),
            "status": "skipped", "error": str(e), "row_index": row_index,
        })
        log.warning("  Fila %d SKIPPED (proveedor no encontrado): %s — %s", row_index, voucher, e)
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

    # Proveedores grandes (> umbral): se cargan en modo CHUNKED (varias facturas de
    # VOUCHER_CHUNK_SIZE c/u, SELECT ALL por chunk). Antes se saltaban; ahora se cargan.
    es_masivo = bool(max_vouchers and max_vouchers > 0 and group["size"] > max_vouchers)

    # Posponer proveedores monstruo: si superan el umbral duro se dejan 'pending' (sin
    # tocar) y se registran, para procesarlos en una corrida dedicada. Evita que un
    # proveedor enorme (ej. 1ING01, 52k registros) bloquee el resto del archivo.
    if MAX_VOUCHERS_DEFER_THRESHOLD and group["size"] > MAX_VOUCHERS_DEFER_THRESHOLD:
        log.warning("  Proveedor %s POSPUESTO: %d registros > umbral %d — queda 'pending' "
                    "para una corrida dedicada",
                    supplier_code, group["size"], MAX_VOUCHERS_DEFER_THRESHOLD)
        if oversized_report is not None:
            oversized_report.append({
                "filename": filename,
                "supplier_code": supplier_code,
                "supplier_name": group.get("supplier_name", ""),
                "voucher_count": group["size"],
                "currencies": ", ".join(group.get("total_by_currency", {}).keys()),
                "totals": "; ".join(f"{c}={t:.2f}" for c, t in group.get("total_by_currency", {}).items()),
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

    if tracker:
        tracker.mark_rows_processing_bulk(filename, all_indices)

    group_ok = True
    group_error = None
    group_timeout = False
    per_row_status: dict[int, str] = {}

    try:
        open_supplier(page, supplier_code)
        navigate_to_transactions(page)

        # Leer referencias INV* ya existentes en TourplanNX para este proveedor.
        # Se corre para TODOS los proveedores (masivos y no-masivos) para prevenir el
        # error 1038 "Reference Exists" que deja el pipeline colgado sin manejar.
        existing_refs = _read_existing_references(page, supplier_code)

        for currency, total in group["total_by_currency"].items():
            # Chequear stop antes de abrir INSERT (no hay modal abierto)
            if stop_event and stop_event.is_set():
                try:
                    exit_supplier(page)
                except Exception:
                    pass
                raise PipelineStopped()

            records_for_currency = [rec for rec in records if rec["currency"] == currency]

            # ── Modo CHUNKED (proveedores masivos): varias facturas, SELECT ALL por chunk ──
            if es_masivo:
                chunks = chunk_records_for_invoice(
                    records_for_currency, VOUCHER_CHUNK_SIZE, VOUCHER_MAX_RANGE_WIDTH)
                log.info("    Moneda %s: total=%.2f, %d vouchers en %d factura(s) (chunked)",
                         currency, total, len(records_for_currency), len(chunks))
                stats.set_activity(currency=currency, voucher_total=len(records_for_currency), voucher_idx=0)
                for ci, chunk in enumerate(chunks, 1):
                    if stop_event and stop_event.is_set():
                        raise PipelineStopped()
                    chunk_ref = f"INV{chunk['records'][0]['row_index']}{supplier_code}"
                    chunk_indices = [r["row_index"] for r in chunk["records"]]
                    log.info("  [DEBUG] Chunk %d/%d ref=%s — existentes: %s",
                              ci, len(chunks), chunk_ref,
                              "SÍ (saltando)" if chunk_ref in existing_refs else "NO (procesar)")
                    if chunk_ref in existing_refs:
                        loaded_vouchers = existing_refs[chunk_ref]
                        log.info("    Factura %d/%d ya existe en TourplanNX (ref=%s, %d vouchers en API) — saltando",
                                 ci, len(chunks), chunk_ref, len(loaded_vouchers))
                        ok_idx, skip_idx = _classify_by_loaded(
                            chunk["records"], loaded_vouchers)
                        for idx in ok_idx:
                            per_row_status[idx] = "ok"
                            stats.add_voucher_result({
                                "filename": filename, "supplier_code": supplier_code,
                                "voucher": "", "currency": currency,
                                "status": "ok", "error": None, "row_index": idx,
                            })
                        for idx in skip_idx:
                            per_row_status[idx] = "skipped"
                            stats.add_voucher_result({
                                "filename": filename, "supplier_code": supplier_code,
                                "voucher": "", "currency": currency,
                                "status": "skipped", "error": "No cargado en corrida anterior",
                                "row_index": idx,
                            })
                        if tracker and filename:
                            tracker.mark_rows_ok_bulk(filename, ok_idx, reference=chunk_ref)
                            tracker.mark_rows_skipped_bulk(filename, skip_idx)
                        continue
                    log.info("    Factura %d/%d: %d vouchers (rango %s-%s)",
                             ci, len(chunks), len(chunk["records"]), chunk["vfrom"], chunk["vto"])
                    ok_idx, failed_idx = _cargar_chunk_factura(
                        page, supplier_code, currency, chunk["records"], select_all=True,
                        tracker=tracker, filename=filename)
                    for idx in ok_idx:
                        per_row_status[idx] = "ok"
                        stats.add_voucher_result({
                            "filename": filename, "supplier_code": supplier_code,
                            "voucher": "", "currency": currency,
                            "status": "ok", "error": None, "row_index": idx,
                        })
                    for idx in failed_idx:
                        per_row_status[idx] = "failed"
                        group_ok = False
                        group_error = group_error or f"Chunk fallido ({currency})"
                continue

            # ── Modo chico (original, una sola factura, matching contra el Excel) ──
            vouchers_for_currency = [rec["voucher"] for rec in records_for_currency]
            reference = f"INV{records_for_currency[0]['row_index']}{supplier_code}"
            log.info("    Moneda %s: total=%.2f, ref=%s, %d vouchers",
                     currency, total, reference, len(records_for_currency))

            # Evitar duplicar si la factura ya existe en TourplanNX
            if reference in existing_refs:
                loaded_vouchers = existing_refs[reference]
                log.info("    Moneda %s ref=%s ya existe en TourplanNX (%d vouchers en API) — saltando",
                         currency, reference, len(loaded_vouchers))
                ok_idx, skip_idx = _classify_by_loaded(records_for_currency, loaded_vouchers)
                for idx in ok_idx:
                    per_row_status[idx] = "ok"
                    stats.add_voucher_result({
                        "filename": filename, "supplier_code": supplier_code,
                        "voucher": "", "currency": currency,
                        "status": "ok", "error": None, "row_index": idx,
                    })
                for idx in skip_idx:
                    per_row_status[idx] = "skipped"
                    stats.add_voucher_result({
                        "filename": filename, "supplier_code": supplier_code,
                        "voucher": "", "currency": currency,
                        "status": "skipped", "error": "No cargado en corrida anterior",
                        "row_index": idx,
                    })
                if tracker and filename:
                    tracker.mark_rows_ok_bulk(filename, ok_idx, reference=reference)
                    tracker.mark_rows_skipped_bulk(filename, skip_idx)
                continue

            stats.set_activity(currency=currency, voucher_total=len(records_for_currency), voucher_idx=0)

            page.locator("#creditorview").get_by_role("button", name="INSERT").click()
            create_bulk_transaction(page, total, currency, reference)
            try:
                confirm_bulk_transaction(page)
            except ReferenceExistsError:
                # TourplanNX rechazó: la referencia ya existe pero no estaba en la API
                # (raro, pero puede pasar si la API tardó o la referencia es de otra corrida).
                log.info("    1038 Reference Exists para %s/%s — marcando ok y saltando",
                         supplier_code, currency)
                try:
                    abort_transaction(page)
                except Exception:
                    pass
                indices = [r["row_index"] for r in records_for_currency]
                for idx in indices:
                    per_row_status[idx] = "ok"
                    stats.add_voucher_result({
                        "filename": filename, "supplier_code": supplier_code,
                        "voucher": "", "currency": currency,
                        "status": "ok", "error": None, "row_index": idx,
                    })
                if tracker and filename:
                    tracker.mark_rows_ok_bulk(filename, indices, reference=reference)
                continue

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
    except SupplierNotFoundError as e:
        # El código de proveedor no existe en TourplanNX: marcar todas sus filas como
        # skipped (no tiene sentido reintentar en corridas futuras).
        log.warning("  Proveedor %s no encontrado en TourplanNX — %d filas skipped", supplier_code, len(all_indices))
        if tracker:
            tracker.mark_rows_skipped_bulk(filename, all_indices)
        if skipped_report is not None:
            skipped_report.append({
                "supplier_code": supplier_code, "reason": str(e),
                "row_indices": all_indices,
            })
        return  # no hace recovery — el sistema está bien, el código simplemente no existe
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
        # Hard recovery: reload completo para limpiar estado de Angular (un goto por
        # hash no reinicia la SPA), luego verificar sesión.
        try:
            page.goto(spa_url("creditor"))
            page.reload(wait_until="networkidle")
            page.wait_for_load_state("networkidle", timeout=15000)
            ensure_logged_in(page, stats)
        except Exception:
            pass

    if group_timeout:
        # Caso chico que dio timeout total en el SEARCH (se trata como oversized).
        if tracker:
            tracker.mark_rows_skipped_bulk(filename, all_indices)
        log.warning("  Proveedor %s marcado oversized por timeout (%d filas skipped)",
                    supplier_code, len(all_indices))
    else:
        # Marcar las filas según su estado real, EN LOTE (chunked y chico comparten esto).
        # Antes era un loop fila-por-fila (un UPDATE+commit por fila) que contra Postgres
        # remoto tardaba ~90s+ por proveedor grande. Las sin estado (no procesadas por un
        # error de navegación) → failed.
        ok_idx = [i for i in all_indices if per_row_status.get(i) == "ok"]
        skip_idx = [i for i in all_indices if per_row_status.get(i) == "skipped"]
        fail_idx = [i for i in all_indices if per_row_status.get(i) not in ("ok", "skipped")]
        if tracker:
            tracker.mark_rows_ok_bulk(filename, ok_idx)
            tracker.mark_rows_skipped_bulk(filename, skip_idx)
            tracker.mark_rows_failed_bulk(filename, fail_idx, group_error or "no procesado")
        ok_n, skip_n, fail_n = len(ok_idx), len(skip_idx), len(fail_idx)
        if fail_n:
            log.warning("  Proveedor %s: %d ok, %d skipped, %d failed", supplier_code, ok_n, skip_n, fail_n)
        else:
            log.info("  Proveedor %s completado: %d ok, %d skipped", supplier_code, ok_n, skip_n)


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

        # ── Fuente de las filas: base o Excel ──────────────────────────────────────
        # Si el archivo ya está cargado en el tracker y su hash coincide (mismo .xlsx
        # sin cambios), se reconstruyen las filas desde la base y se evita re-parsear el
        # Excel. Si cambió o es nuevo, se lee el Excel. get_all_records devuelve None para
        # filas pre-migración (sin product_cost) → fallback a Excel para no perder montos.
        rows = None
        reused_from_db = False
        fstatus = None
        current_hash = None
        if tracker:
            fstatus = tracker.get_file_status(filename)
            current_hash = tracker.file_hash(filepath)
            if fstatus and fstatus.get("file_hash") == current_hash:
                db_rows = tracker.get_all_records(filename)
                if db_rows:
                    rows = db_rows
                    reused_from_db = True
                    log.info("Procesando archivo: %s (%d filas desde la base, sin releer Excel)",
                             filename, len(rows))
                    stats.set_activity(file=filename)

        if rows is None:
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
                log.warning("  Sin datos en %s, moviendo a processed/", filename)
                shutil.move(str(filepath), str(PROCESSED_DIR / filename))
                continue

        if tracker:
            if not reused_from_db:
                # init_rows sobre archivos enormes contra Postgres remoto es lentísimo
                # (593k filas ≈ 10 min). Si el archivo ya está cargado (mismo hash y la
                # cantidad de filas coincide), se omite: las filas y sus estados ya están
                # en el tracker. El parseo del Excel de arriba igual se necesita para los
                # montos (que no se guardan en filas pre-migración).
                already_loaded = (
                    fstatus is not None
                    and fstatus.get("file_hash") == current_hash
                    and tracker.count_rows(filename) >= len(rows)
                )
                if already_loaded:
                    log.info("  Filas ya cargadas en el tracker (%d) — se omite init_rows", len(rows))
                else:
                    tracker.mark_file_pending(filename, current_hash, len(rows))
                    tracker.init_rows(filename, rows)
            tracker.mark_file_processing(filename)
            # Recuperar grupos interrumpidos por una caída previa (processing → pending)
            recovered = tracker.reset_processing_to_pending(filename)
            if recovered:
                log.info("  Recuperadas %d filas 'processing' → 'pending' (corrida previa interrumpida)", recovered)
            # Reintentar las filas 'failed' y 'skipped' una sola vez por ejecución
            reintentos = tracker.reset_failed_to_pending(filename)
            if reintentos:
                log.info("  Reintentando %d filas 'failed' → 'pending' (1 vez por ejecución)", reintentos)
            skipped_retry = tracker.reset_skipped_to_pending(filename)
            if skipped_retry:
                log.info("  Reintentando %d filas 'skipped' → 'pending' (1 vez por ejecución)", skipped_retry)
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

        # Determinar si mover el archivo o dejarlo en input/.
        # Las filas 'failed' también retienen el archivo en input/ para reintentarlas
        # en la próxima ejecución (reset_failed_to_pending al reiniciar).
        remaining = tracker.get_pending_rows(filename) if tracker else []
        failed_restantes = tracker.count_failed_rows(filename) if tracker else 0
        stopped = pipeline_stopped or bool(stop_event and stop_event.is_set())

        if remaining or failed_restantes or stopped:
            if tracker:
                tracker.mark_file_processing(filename)
            log.info("Archivo %s incompleto: %d pendientes, %d failed (reintenta próxima corrida) — permanece en input/",
                     filename, len(remaining), failed_restantes)
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
