import asyncio
import json
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config.settings import (
    LOG_DIR,
    BASE_DIR,
    DB_CONNECTION,
    DB_HOST,
    DB_PORT,
    DB_DATABASE,
    DB_USERNAME,
    DB_PASSWORD,
)
from core.pipeline import INPUT_DIR
from data.tracker import ProcessTracker
from main import run_manager

app = FastAPI(title="Monitor de Automatización")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _get_tracker_conn():
    """Devuelve (connection, db_type). El caller debe cerrar la conexión."""
    if DB_CONNECTION == "pgsql":
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(
            host=DB_HOST,
            port=int(DB_PORT),
            dbname=DB_DATABASE,
            user=DB_USERNAME,
            password=DB_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        return conn, "pgsql"
    import sqlite3

    db_path = BASE_DIR / "outputs" / "tracker.db"
    if not db_path.exists():
        return None, "sqlite"
    conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, "sqlite"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _load_html() -> str:
    return (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _load_html()


@app.get("/api/stats")
async def get_stats():
    return run_manager.snapshot()


@app.get("/api/stream")
async def stream_stats(request: Request):
    async def event_generator():
        last_seq = 0
        last_voucher_seq = 0
        while True:
            if await request.is_disconnected():
                break
            snap = run_manager.snapshot()
            s = run_manager.stats
            new_events = s.events_after(last_seq) if s else []
            new_vouchers = s.vouchers_after(last_voucher_seq) if s else []
            if new_events:
                last_seq = new_events[-1]["seq"]
            if new_vouchers:
                last_voucher_seq = new_vouchers[-1]["seq"]
            snap["events"] = new_events
            snap["vouchers"] = new_vouchers
            yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _build_run_options(request: Request) -> dict:
    qp = request.query_params
    truthy = lambda k: qp.get(k, "").lower() in ("true", "1", "yes")
    opts: dict = {
        "headless": not truthy("visible"),
        "no_tracker": truthy("no_tracker"),
    }
    if truthy("test"):
        cfg: dict = {"test": True, "sheet": qp.get("sheet") or "SI TRANS"}
        raw_row = qp.get("row", "").strip()
        if raw_row:
            try:
                cfg["row"] = int(raw_row)
            except ValueError:
                pass
        supplier = qp.get("supplier", "").strip()
        if supplier:
            cfg["supplier"] = supplier
        opts["test_config"] = cfg
    return opts


@app.post("/api/start")
async def start_automation(request: Request):
    opts = _build_run_options(request)
    ok, msg = run_manager.start("full", **opts)
    return {"status": "ok" if ok else "error", "message": msg}


@app.post("/api/start/pipeline")
async def start_pipeline(request: Request):
    opts = _build_run_options(request)
    ok, msg = run_manager.start("pipeline", **opts)
    return {"status": "ok" if ok else "error", "message": msg}


@app.post("/api/start/cheques")
async def start_cheques(request: Request):
    opts = _build_run_options(request)
    ok, msg = run_manager.start("cheques", **opts)
    return {"status": "ok" if ok else "error", "message": msg}


@app.post("/api/stop")
async def stop_automation_endpoint(force: bool = False):
    if force:
        ok, msg = await run_in_threadpool(run_manager.force_stop)
    else:
        ok, msg = run_manager.stop()
    return {"status": "ok" if ok else "error", "message": msg}


@app.get("/api/logs")
async def get_logs(lines: int = 100):
    log_path = Path(LOG_DIR) / "automation.log"
    if not log_path.exists():
        return {"logs": []}

    def _read():
        with log_path.open(encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n") for l in deque(f, maxlen=lines)]

    content = await run_in_threadpool(_read)
    return {"logs": content}


@app.get("/api/tracker")
async def get_tracker():
    def _fetch():
        tracker = ProcessTracker()
        summary = tracker.get_summary()
        pending = []
        if INPUT_DIR.exists():
            pending = [f.name for f in sorted(INPUT_DIR.glob("*.xlsx"))]
        return {"files": summary, "pending": pending}

    return await run_in_threadpool(_fetch)


@app.get("/api/sheets")
async def get_sheets():
    """Devuelve las hojas de cada xlsx en input/. Solo se llama on-demand."""
    def _fetch():
        result = {}
        if INPUT_DIR.exists():
            for f in sorted(INPUT_DIR.glob("*.xlsx")):
                try:
                    import pandas as pd
                    xls = pd.ExcelFile(f)
                    result[f.name] = [s for s in xls.sheet_names if s not in ("Sheet2",)]
                except Exception:
                    result[f.name] = []
        return result

    return await run_in_threadpool(_fetch)


@app.get("/api/history")
async def get_history(limit: int = 100, offset: int = 0):
    """Devuelve una página de vouchers procesados desde la DB (más recientes primero).

    Paginado con LIMIT/OFFSET para no cargar toda la tabla de golpe — apoyado en el
    índice idx_rows_processed_at. El total real solo se cuenta en la primera página
    (offset == 0) para evitar un COUNT(*) en cada scroll."""
    limit = max(1, min(limit, 500))  # cota defensiva
    offset = max(0, offset)

    def _fetch():
        conn, db_type = _get_tracker_conn()
        if conn is None:
            return {"vouchers": [], "total": 0, "offset": offset,
                    "limit": limit, "has_more": False}

        def _row_to_dict(r):
            ts_raw = r["processed_at"] or ""
            return {
                "id": f"{r['filename']}:{r['row_index']}",
                "date": ts_raw[5:10] if len(ts_raw) >= 10 else "",
                "ts": ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw,
                "supplier_code": r["supplier_code"] or "",
                "voucher": r["booking_reference"] or "",
                "currency": r["currency"] or "",
                "status": r["status"],
                "error": r["error"] or "",
                "filename": r["filename"],
                "source": "history",
            }

        PAGE_SQL = (
            "SELECT filename, row_index, booking_reference, supplier_code, currency, "
            "status, error, processed_at "
            "FROM processed_rows "
            "WHERE status IN ('ok', 'failed', 'skipped') "
            "ORDER BY processed_at DESC "
            "LIMIT %s OFFSET %s"
        )
        COUNT_SQL = (
            "SELECT COUNT(*) AS cnt FROM processed_rows "
            "WHERE status IN ('ok', 'failed', 'skipped')"
        )

        try:
            if db_type == "pgsql":
                cur = conn.cursor()
                cur.execute(PAGE_SQL, (limit, offset))
                rows = cur.fetchall()
                total = None
                if offset == 0:
                    cur.execute(COUNT_SQL)
                    total = cur.fetchone()["cnt"]
            else:
                sql = PAGE_SQL.replace("%s", "?")
                rows = conn.execute(sql, (limit, offset)).fetchall()
                total = None
                if offset == 0:
                    total = conn.execute(COUNT_SQL).fetchone()["cnt"]
            conn.close()

            vouchers = [_row_to_dict(r) for r in rows]
            return {
                "vouchers": vouchers,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": len(vouchers) == limit,
            }

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return {"vouchers": [], "total": 0, "offset": offset,
                    "limit": limit, "has_more": False, "error": str(e)}

    return await run_in_threadpool(_fetch)


@app.get("/api/report")
async def get_report():
    """Resumen agregado por proveedor. PostgreSQL: GROUP BY directo con STRING_AGG.
    SQLite: GROUP BY con fallback por chunks si la DB está corrupta."""
    def _fetch():
        conn, db_type = _get_tracker_conn()
        if conn is None:
            return {"suppliers": [], "totals": {}}

        try:
            if db_type == "pgsql":
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        supplier_code,
                        COUNT(*) AS total,
                        SUM(CASE WHEN status='ok'      THEN 1 ELSE 0 END) AS ok,
                        SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
                        SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                        STRING_AGG(DISTINCT currency, ',') AS currencies,
                        MAX(processed_at) AS last_processed
                    FROM processed_rows
                    WHERE supplier_code IS NOT NULL AND supplier_code != ''
                    GROUP BY supplier_code
                    ORDER BY total DESC
                """)
                suppliers = [dict(r) for r in cur.fetchall()]
                conn.close()
            else:
                # SQLite: GROUP BY directo primero
                try:
                    rows = conn.execute("""
                        SELECT
                            supplier_code,
                            COUNT(*) AS total,
                            SUM(CASE WHEN status='ok'      THEN 1 ELSE 0 END) AS ok,
                            SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed,
                            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
                            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                            GROUP_CONCAT(DISTINCT currency) AS currencies,
                            MAX(processed_at) AS last_processed
                        FROM processed_rows
                        WHERE supplier_code IS NOT NULL AND supplier_code != ''
                        GROUP BY supplier_code
                        ORDER BY total DESC
                    """).fetchall()
                    conn.close()
                    suppliers = [dict(r) for r in rows]
                except Exception:
                    # SQLite fallback: scan por chunks, agrega en Python
                    agg: dict = {}
                    CHUNK = 5000
                    offset = 0
                    while True:
                        try:
                            chunk = conn.execute(
                                "SELECT supplier_code, currency, status, processed_at "
                                "FROM processed_rows LIMIT ? OFFSET ?",
                                (CHUNK, offset),
                            ).fetchall()
                        except Exception:
                            break
                        if not chunk:
                            break
                        for r in chunk:
                            sup = r["supplier_code"] or ""
                            if not sup:
                                continue
                            if sup not in agg:
                                agg[sup] = {
                                    "supplier_code": sup, "total": 0, "ok": 0,
                                    "failed": 0, "skipped": 0, "pending": 0,
                                    "currencies": set(), "last_processed": "",
                                }
                            e = agg[sup]
                            e["total"] += 1
                            st = r["status"] or "pending"
                            if st in e:
                                e[st] += 1
                            if r["currency"]:
                                e["currencies"].add(r["currency"])
                            ts = r["processed_at"] or ""
                            if ts > e["last_processed"]:
                                e["last_processed"] = ts
                        offset += CHUNK
                    conn.close()
                    suppliers = sorted(
                        [{**s, "currencies": ",".join(sorted(s["currencies"]))} for s in agg.values()],
                        key=lambda x: x["total"],
                        reverse=True,
                    )
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return {"suppliers": [], "totals": {}, "error": str(e)}

        totals: dict = {
            "total": 0, "ok": 0, "failed": 0, "skipped": 0,
            "pending": 0, "suppliers": len(suppliers),
        }
        for s in suppliers:
            for k in ("total", "ok", "failed", "skipped", "pending"):
                totals[k] += s.get(k, 0)

        return {"suppliers": suppliers, "totals": totals}

    return await run_in_threadpool(_fetch)


@app.get("/api/report/csv")
async def get_report_csv():
    """Descarga el reporte de proveedores como CSV (UTF-8 con BOM para Excel)."""
    import csv
    import io
    from datetime import date
    from fastapi.responses import StreamingResponse as SR

    data = await get_report()

    def generate():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Proveedor", "Total", "OK", "Fallidos", "Saltados", "Pendientes", "Monedas", "Último proceso"])
        for s in data.get("suppliers", []):
            w.writerow([
                s["supplier_code"],
                s["total"],
                s["ok"],
                s["failed"],
                s["skipped"],
                s["pending"],
                s["currencies"] or "",
                (s["last_processed"] or "")[:16],
            ])
        yield buf.getvalue().encode("utf-8-sig")

    fname = f"reporte_proveedores_{date.today().strftime('%Y%m%d')}.csv"
    return SR(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.post("/api/tracker/reset")
async def reset_tracker(file: str = "", all: bool = False):
    def _reset():
        tracker = ProcessTracker()
        if all:
            tracker.reset_all()
            return {"status": "ok", "message": "Tracker reseteado completamente"}
        if file:
            tracker.reset_file(file)
            return {"status": "ok", "message": f"Tracker reseteado para: {file}"}
        return {"status": "error", "message": "Especificá ?all=true o ?file=nombre.xlsx"}

    return await run_in_threadpool(_reset)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("monitor.app:app", host="0.0.0.0", port=8000, reload=True)
