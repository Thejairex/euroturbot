"""Filtro de vouchers por estatus desde el tracker.

Paso posterior a la carga: una vez que el pipeline marcó cada fila en el tracker
(ok/failed/skipped), este módulo permite traer los vouchers que quedaron en un
estatus dado — por defecto 'ok' — para alimentar el chequeo posterior.

Es independiente del pipeline de carga: solo lee el tracker (read-only).

Nota: el número de voucher se almacena en la columna `booking_reference` del
tracker (ver data/tracker.py::init_rows), por eso acá se expone como `voucher_number`.
"""
from typing import Any

from data.tracker import ProcessTracker


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Normaliza una fila del tracker a la forma que usa el chequeo."""
    return {
        "filename": row.get("filename"),
        "row_index": row.get("row_index"),
        "voucher_number": row.get("booking_reference"),
        "supplier_code": row.get("supplier_code"),
        "currency": row.get("currency"),
        "status": row.get("status"),
        "processed_at": row.get("processed_at"),
    }


def get_vouchers_by_status(
    status: str = "ok",
    filename: str | None = None,
    tracker: ProcessTracker | None = None,
) -> list[dict[str, Any]]:
    """Trae los vouchers con un estatus dado (por defecto 'ok').

    Args:
        status: estatus a filtrar (ok/failed/skipped/pending/processing).
        filename: si se indica, acota a ese archivo; si es None, todos.
        tracker: instancia existente de ProcessTracker; si es None, abre una nueva
                 y la cierra al terminar.

    Returns:
        Lista de dicts normalizados con voucher_number, supplier_code, currency, etc.
    """
    own_tracker = tracker is None
    tr = tracker or ProcessTracker()
    try:
        rows = tr.get_rows_by_status(status, filename)
        return [_normalize(r) for r in rows]
    finally:
        if own_tracker:
            tr.close()


def get_ok_vouchers(
    filename: str | None = None, tracker: ProcessTracker | None = None
) -> list[dict[str, Any]]:
    """Atajo: trae los vouchers con estatus 'ok'."""
    return get_vouchers_by_status("ok", filename, tracker)
