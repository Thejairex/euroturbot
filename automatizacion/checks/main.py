"""CLI del módulo de chequeos.

Uso (cwd = automatizacion/):
    python -m checks.main                      # vouchers OK de todos los archivos
    python -m checks.main --status failed      # filtrar por otro estatus
    python -m checks.main --file "archivo.xlsx" # acotar a un archivo
    python -m checks.main --count              # solo contar, no listar
"""
import argparse

from checks.voucher_filter import get_vouchers_by_status
from data.tracker import ProcessTracker


def main() -> None:
    parser = argparse.ArgumentParser(description="Filtro de vouchers por estatus")
    parser.add_argument(
        "--status", default="ok",
        help="Estatus a filtrar (ok/failed/skipped/pending/processing). Default: ok",
    )
    parser.add_argument(
        "--file", default=None,
        help="Acotar a un archivo (nombre exacto). Default: todos",
    )
    parser.add_argument(
        "--count", action="store_true",
        help="Solo mostrar el total, sin listar cada voucher",
    )
    args = parser.parse_args()

    tracker = ProcessTracker()
    try:
        if args.count:
            n = tracker.count_rows_by_status(args.status, args.file)
            print(f"Vouchers con estatus '{args.status}': {n}")
            return

        vouchers = get_vouchers_by_status(args.status, args.file, tracker=tracker)
        print(f"Vouchers con estatus '{args.status}': {len(vouchers)}\n")
        for v in vouchers:
            print(
                f"  [{v['row_index']:>5}] voucher={v['voucher_number']!s:>12}  "
                f"proveedor={v['supplier_code']!s:<10}  moneda={v['currency']!s:<4}  "
                f"({v['filename']})"
            )
    finally:
        tracker.close()


if __name__ == "__main__":
    main()
