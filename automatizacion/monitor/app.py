import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template

from core.stats import StatsTracker
from main import run_automation_thread
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


@app.post("/api/start")
async def start_automation():
    global stats
    if _automation_lock.locked():
        return {"status": "error", "message": "Ya hay una automatización en ejecución"}

    stats = StatsTracker()
    run_automation_thread(stats, headless=True)
    return {"status": "ok", "message": "Automatización iniciada"}


@app.get("/api/logs")
async def get_logs(lines: int = 50):
    log_path = Path("outputs") / "logs" / "automation.log"
    if not log_path.exists():
        return {"logs": []}
    content = log_path.read_text(encoding="utf-8").splitlines()
    return {"logs": content[-lines:]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("monitor.app:app", host="0.0.0.0", port=8000, reload=True)
