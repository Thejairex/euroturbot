"""Reporte de remediación: facturas guardadas con el total DESCUADRADO.

Parsea el log de la automatización y lista cada invoice que se guardó pese a que el
INVOICE TOTAL no coincidía con el EXPECTED TOTAL (bug de sobre-selección: el viejo
SELECT ALL del modo masivo barría vouchers ajenos dentro del rango numérico y se guardaba
igual confirmando el warning "Invoice total mismatch" con YES).

Es READ-ONLY: solo lee el log y escribe un CSV. No toca TourplanNX ni la base.

Uso (cwd = automatizacion/):
    python reportar_descuadres.py
    python reportar_descuadres.py --log outputs/logs/automation.log --min-remainder 1.0
    python reportar_descuadres.py --out outputs/reports/mi_reporte.csv

Cada fila del CSV = una factura guardada mal, con:
    timestamp, supplier_code, reference, currency, invoice_total, expected_total,
    remainder, found, selected, source_line
Ordenado por |remainder| descendente (las peores primero).
"""
import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_LOG = BASE / "outputs" / "logs" / "automation.log"
DEFAULT_OUT_DIR = BASE / "outputs" / "reports"

# ── Patrones del log ──────────────────────────────────────────────────────────────
RE_SUPPLIER = re.compile(r"Proveedor (\S+) —")
RE_SUPPLIER_ALT = re.compile(r"Buscando proveedor: (\S+)")
# ref + total + moneda desde el formulario masivo o la línea de moneda del modo chico
RE_REF_MASIVO = re.compile(r"Llenando formulario masivo \(total=(-?[\d.]+), currency=(\w+), ref=(INV\S+?)\)")
RE_REF_CHICO = re.compile(r"Moneda (\w+): total=(-?[\d.]+), ref=(INV\S+?),")
RE_TOTALS = re.compile(r"INVOICE=(-?[\d.]+) EXPECTED=(-?[\d.]+) REMAINDER=(-?[\d.]+)")
RE_FOUND = re.compile(r"SEARCH resultados: Found=(\d+)")
RE_SELECTALL = re.compile(r"SELECT ALL: (\d+) vouchers")
RE_YES = re.compile(r"Warning de descuadre confirmado \(YES\)")
RE_SAVED = re.compile(r"Invoice guardado \(SAVE\)|Factura guardada:")
RE_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def parse_log(log_path: Path, min_remainder: float):
    """Recorre el log y devuelve la lista de facturas guardadas con |remainder| > min_remainder."""
    supplier = ""
    ref = currency = ""
    invoice_total = expected_total = remainder = None
    found = selected = None
    yes_seen = False
    ts = ""
    rows = []

    with log_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = RE_TS.match(line)
            if m:
                ts = m.group(1)

            ms = RE_SUPPLIER.search(line) or RE_SUPPLIER_ALT.search(line)
            if ms:
                supplier = ms.group(1)

            mr = RE_REF_MASIVO.search(line)
            if mr:
                # nueva factura: resetear estado por-factura
                currency, ref = mr.group(2), mr.group(3)
                invoice_total = expected_total = remainder = None
                found = selected = None
                yes_seen = False
                continue
            mc = RE_REF_CHICO.search(line)
            if mc:
                currency, ref = mc.group(1), mc.group(3)
                invoice_total = expected_total = remainder = None
                found = selected = None
                yes_seen = False
                continue

            mf = RE_FOUND.search(line)
            if mf:
                found = int(mf.group(1))
            msa = RE_SELECTALL.search(line)
            if msa:
                selected = int(msa.group(1))
            mt = RE_TOTALS.search(line)
            if mt:
                invoice_total = float(mt.group(1))
                expected_total = float(mt.group(2))
                remainder = float(mt.group(3))
            if RE_YES.search(line):
                yes_seen = True

            if RE_SAVED.search(line):
                # Se guardó. ¿Estaba descuadrado?
                descuadrado = (remainder is not None and abs(remainder) > min_remainder) or yes_seen
                if descuadrado and ref:
                    rows.append({
                        "timestamp": ts,
                        "supplier_code": supplier,
                        "reference": ref,
                        "currency": currency,
                        "invoice_total": invoice_total if invoice_total is not None else "",
                        "expected_total": expected_total if expected_total is not None else "",
                        "remainder": remainder if remainder is not None else "",
                        "found": found if found is not None else "",
                        "selected": selected if selected is not None else "",
                        "confirmado_yes": "SI" if yes_seen else "",
                    })
                # reset por-factura tras guardar (el próximo ref abrirá una nueva)
                invoice_total = expected_total = remainder = None
                found = selected = None
                yes_seen = False

    return rows


def main():
    ap = argparse.ArgumentParser(description="Reporte de facturas guardadas con descuadre.")
    ap.add_argument("--log", type=str, default=str(DEFAULT_LOG), help="Ruta del automation.log")
    ap.add_argument("--out", type=str, default=None, help="Ruta del CSV de salida")
    ap.add_argument("--min-remainder", type=float, default=0.01,
                    help="Umbral |remainder| para considerar descuadrada una factura (default 0.01)")
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"No existe el log: {log_path}")
        return 1

    rows = parse_log(log_path, args.min_remainder)
    # Ordenar por |remainder| desc (peores primero); las sin remainder numérico van al final.
    def _key(r):
        try:
            return abs(float(r["remainder"]))
        except (ValueError, TypeError):
            return -1.0
    rows.sort(key=_key, reverse=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else (DEFAULT_OUT_DIR / f"facturas_descuadradas_{stamp}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fields = ["timestamp", "supplier_code", "reference", "currency", "invoice_total",
              "expected_total", "remainder", "found", "selected", "confirmado_yes"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    grandes = sum(1 for r in rows if _key(r) >= 100)
    print(f"Facturas descuadradas encontradas: {total}")
    print(f"  con |remainder| >= 100: {grandes}")
    if rows:
        print(f"  peor caso: {rows[0]['reference']} REMAINDER={rows[0]['remainder']} "
              f"(INVOICE={rows[0]['invoice_total']} EXPECTED={rows[0]['expected_total']})")
    print(f"CSV: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
