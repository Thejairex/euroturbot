import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config.settings import REPORT_DIR


def _parse_cost(value) -> float:
    """Convierte un string de costo a float, tolerando comas y espacios."""
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


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
                    {"row_index": int, "voucher": str, "currency": str, "product_cost": float}
                ],
                "size": int,
                "total_by_currency": {"ARS": 245601.01, "USD": 3135.0},
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

        currency = (row.get("Service_Cost_Currency") or "").strip()
        product_cost = _parse_cost(row.get("ProductCost"))

        if "MEP" in currency.upper():
            groups[code].setdefault("skipped_mep", []).append(i)
            continue

        groups[code]["records"].append({
            "row_index": i,
            "voucher": str(row.get("Voucher_Number") or ""),
            "currency": currency,
            "product_cost": product_cost,
        })

    result = []
    for code in order:
        g = groups[code]
        g.setdefault("skipped_mep", [])
        g["size"] = len(g["records"])
        totals: dict[str, float] = defaultdict(float)
        for rec in g["records"]:
            totals[rec["currency"]] += rec["product_cost"]
        g["total_by_currency"] = dict(totals)
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
            first_index_by_currency: dict[str, int] = {}
            for rec in g["records"]:
                if rec["currency"] not in first_index_by_currency:
                    first_index_by_currency[rec["currency"]] = rec["row_index"]
            for rec in g["records"]:
                reference = f"INV{first_index_by_currency[rec['currency']]}{g['supplier_code']}"
                writer.writerow([
                    g["supplier_code"],
                    g["supplier_name"],
                    rec["voucher"],
                    rec["currency"],
                    reference,
                    g["size"],
                    "SI" if g["size"] > 1 else "NO",
                    rec["row_index"],
                ])

    # --- CSV resumen (una fila por proveedor) ---
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Supplier_Code", "Supplier_Name",
            "voucher_count", "vouchers", "currencies",
            "expected_total_by_currency", "skipped_mep_rows", "Reference",
        ])
        for g in groups:
            vouchers = " | ".join(r["voucher"] for r in g["records"])
            currencies = " | ".join(sorted({r["currency"] for r in g["records"]}))
            totals_str = " | ".join(
                f"{cur}: {total:.2f}"
                for cur, total in sorted(g["total_by_currency"].items())
            )
            skipped = len(g.get("skipped_mep", []))
            # Reference del primer invoice (primer record de cada moneda; aquí usamos el primer record del grupo)
            first_rec = g["records"][0] if g["records"] else None
            summary_reference = f"INV{first_rec['row_index']}{g['supplier_code']}" if first_rec else ""
            writer.writerow([
                g["supplier_code"],
                g["supplier_name"],
                g["size"],
                vouchers,
                currencies,
                totals_str,
                skipped if skipped else "",
                summary_reference,
            ])

    return detail_path, summary_path


def write_skipped_report(skipped: list[dict]) -> Path | None:
    """Escribe un CSV con los vouchers salteados por cuenta inválida.

    Cada entrada de `skipped` debe tener:
        filename, supplier_code, supplier_name, currency,
        voucher, account, reason, row_index
    """
    if not skipped:
        return None
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"vouchers_salteados_{stamp}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Archivo", "Supplier_Code", "Supplier_Name",
            "Service_Cost_Currency", "Voucher_Number",
            "Cuenta_Invalida", "Motivo", "original_row_index",
        ])
        for s in skipped:
            writer.writerow([
                s.get("filename", ""),
                s.get("supplier_code", ""),
                s.get("supplier_name", ""),
                s.get("currency", ""),
                s.get("voucher", ""),
                s.get("account", "?"),
                s.get("reason", ""),
                s.get("row_index", ""),
            ])
    return path
