import csv
from pathlib import Path

from config.settings import REPORT_DIR
from modules.transaction_creator import TRANSACTION_REFERENCE


def group_rows_by_supplier(rows: list[dict], pending_indices: list[int] | None = None) -> list[dict]:
    """Agrupa filas por Supplier_Code preservando el orden de aparición.

    Args:
        rows: lista completa de dicts leídos del Excel (índice == posición en lista).
        pending_indices: si se pasa, solo incluye los row_index indicados.
            None = incluir todos.

    Returns:
        Lista de grupos ordenada por primera aparición del proveedor:
        [
            {
                "supplier_code": str,
                "supplier_name": str,
                "records": [
                    {"row_index": int, "voucher": str, "currency": str}
                ],
                "size": int,
            }
        ]
    """
    indices = set(pending_indices) if pending_indices is not None else None

    groups: dict[str, dict] = {}
    order: list[str] = []

    for i, row in enumerate(rows):
        if indices is not None and i not in indices:
            continue

        code = (row.get("Supplier_Code") or "").strip()
        if not code:
            continue

        if code not in groups:
            groups[code] = {
                "supplier_code": code,
                "supplier_name": (row.get("Supplier_Name") or "").strip(),
                "records": [],
            }
            order.append(code)

        groups[code]["records"].append({
            "row_index": i,
            "voucher": str(row.get("Voucher_Number") or ""),
            "currency": (row.get("Service_Cost_Currency") or "").strip(),
        })

    result = []
    for code in order:
        g = groups[code]
        g["size"] = len(g["records"])
        result.append(g)
    return result


def export_grouped_csv(filepath: Path, sheet_name: str | None = None) -> tuple[Path, Path]:
    """Lee el Excel y escribe dos CSV en outputs/reports/.

    Returns:
        (detail_path, summary_path)
    """
    from core.pipeline import get_data_rows, get_sheet_names

    if sheet_name is None:
        sheets = get_sheet_names(filepath)
        sheet_name = sheets[0] if sheets else None
    if not sheet_name:
        raise ValueError(f"No se encontraron hojas válidas en {filepath.name}")

    rows = get_data_rows(filepath, sheet_name)
    if not rows:
        raise ValueError(f"Sin datos en {filepath.name} / {sheet_name}")

    groups = group_rows_by_supplier(rows)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = REPORT_DIR / "grouped_detail.csv"
    summary_path = REPORT_DIR / "grouped_summary.csv"

    # --- CSV detalle (una fila por registro) ---
    with open(detail_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Supplier_Code", "Supplier_Name",
            "Voucher_Number", "Service_Cost_Currency",
            "Reference", "group_size", "is_bulk", "original_row_index",
        ])
        for g in groups:
            for rec in g["records"]:
                writer.writerow([
                    g["supplier_code"],
                    g["supplier_name"],
                    rec["voucher"],
                    rec["currency"],
                    TRANSACTION_REFERENCE,
                    g["size"],
                    "SI" if g["size"] > 1 else "NO",
                    rec["row_index"],
                ])

    # --- CSV resumen (una fila por proveedor) ---
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Supplier_Code", "Supplier_Name",
            "voucher_count", "vouchers", "currencies", "Reference",
        ])
        for g in groups:
            vouchers = " | ".join(r["voucher"] for r in g["records"])
            currencies = " | ".join(sorted({r["currency"] for r in g["records"]}))
            writer.writerow([
                g["supplier_code"],
                g["supplier_name"],
                g["size"],
                vouchers,
                currencies,
                TRANSACTION_REFERENCE,
            ])

    return detail_path, summary_path
