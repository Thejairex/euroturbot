import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.stats import StatsTracker
from data.tracker import ProcessTracker
from core.pipeline import INPUT_DIR
from main import run_automation_thread, run_pipeline_thread, stop_automation
from utils.logger import log

app = FastAPI(title="Monitor de Automatización")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

stats = StatsTracker()
_automation_lock = asyncio.Lock()


def _load_html() -> str:
    path = TEMPLATES_DIR / "dashboard.html"
    return path.read_text(encoding="utf-8")


HTML_CACHE = _load_html()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTML_CACHE


@app.get("/api/stats")
async def get_stats():
    return stats.results


@app.get("/api/stream")
async def stream_stats(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            data = stats.results
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            if data.get("finished"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _build_test_config(request: Request) -> dict | None:
    test = request.query_params.get("test", "").lower() in ("true", "1", "yes")
    if not test:
        return None
    cfg: dict = {"test": True, "sheet": request.query_params.get("sheet", "SI TRANS")}
    row = request.query_params.get("row")
    if row is not None and row.strip():
        try:
            cfg["row"] = int(row)
        except ValueError:
            pass
    return cfg


@app.post("/api/start")
async def start_automation(request: Request):
    global stats
    if _automation_lock.locked():
        return {"status": "error", "message": "Ya hay una automatización en ejecución"}

    test_config = _build_test_config(request)
    stats = StatsTracker()
    run_automation_thread(stats, headless=True, test_config=test_config)
    return {"status": "ok", "message": "Automatización iniciada"}


@app.post("/api/start/pipeline")
async def start_pipeline(request: Request):
    global stats
    if _automation_lock.locked():
        return {"status": "error", "message": "Ya hay una automatización en ejecución"}

    test_config = _build_test_config(request)
    stats = StatsTracker()
    run_pipeline_thread(stats, headless=True, test_config=test_config)
    return {"status": "ok", "message": "Pipeline iniciado"}


@app.post("/api/stop")
async def stop_automation_endpoint():
    stop_automation()
    return {"status": "ok", "message": "Deteniendo... el pipeline se detendrá al finalizar la fila actual"}


@app.get("/api/logs")
async def get_logs(lines: int = 50):
    log_path = Path("outputs") / "logs" / "automation.log"
    if not log_path.exists():
        return {"logs": []}
    content = log_path.read_text(encoding="utf-8").splitlines()
    return {"logs": content[-lines:]}


@app.get("/api/tracker")
async def get_tracker():
    tracker = ProcessTracker()
    summary = tracker.get_summary()
    pending = []
    if INPUT_DIR.exists():
        pending = [f.name for f in sorted(INPUT_DIR.glob("*.xlsx"))]
    sheets = []
    for f in sorted(INPUT_DIR.glob("*.xlsx")):
        try:
            import pandas as pd
            xls = pd.ExcelFile(f)
            sheets = [s for s in xls.sheet_names if s not in ("Sheet2",)]
        except Exception:
            pass
    return {"files": summary, "pending": pending, "sheets": sheets}


@app.post("/api/tracker/reset")
async def reset_tracker(file: str = "", all: bool = False):
    tracker = ProcessTracker()
    if all:
        tracker.reset_all()
        return {"status": "ok", "message": "Tracker reseteado completamente"}
    if file:
        tracker.reset_file(file)
        return {"status": "ok", "message": f"Tracker reseteado para: {file}"}
    return {"status": "error", "message": "Especificá ?all=true o ?file=nombre.xlsx"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("monitor.app:app", host="0.0.0.0", port=8000, reload=True)
