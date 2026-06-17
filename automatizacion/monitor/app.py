import asyncio
import json
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config.settings import LOG_DIR, BASE_DIR
from core.pipeline import INPUT_DIR
from data.tracker import ProcessTracker
from main import run_manager

app = FastAPI(title="Monitor de Automatización")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

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
async def get_history(limit: int = 500):
    """Devuelve los últimos N vouchers procesados de sesiones anteriores (desde tracker.db)."""
    def _fetch():
        import sqlite3
        db_path = BASE_DIR / "outputs" / "tracker.db"
        if not db_path.exists():
            return {"vouchers": []}
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT filename, row_index, booking_reference, supplier_code, currency, "
                "status, error, processed_at "
                "FROM processed_rows "
                "WHERE status IN ('ok', 'failed', 'skipped') "
                "ORDER BY processed_at DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        except Exception:
            return {"vouchers": [], "error": "tracker.db no disponible"}
        result = []
        for r in rows:
            ts_raw = r["processed_at"] or ""
            # "YYYY-MM-DD HH:MM:SS" → date="MM-DD", ts="HH:MM:SS"
            date_part = ts_raw[5:10] if len(ts_raw) >= 10 else ""
            time_part = ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw
            result.append({
                "id": f"{r['filename']}:{r['row_index']}",
                "date": date_part,
                "ts": time_part,
                "supplier_code": r["supplier_code"] or "",
                "voucher": r["booking_reference"] or "",
                "currency": r["currency"] or "",
                "status": r["status"],
                "error": r["error"] or "",
                "filename": r["filename"],
                "source": "history",
            })
        return {"vouchers": result}

    return await run_in_threadpool(_fetch)


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
