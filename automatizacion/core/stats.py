import time
import json
from threading import Lock
from dataclasses import dataclass
from pathlib import Path

from config.settings import REPORT_DIR


@dataclass
class StepStats:
    name: str
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    @property
    def duration(self) -> float:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 2)
        return 0.0


class StatsTracker:
    def __init__(self):
        self._steps: list[StepStats] = []
        self._started_at: float | None = None
        self._lock = Lock()
        self._finished = False
        self._error: str | None = None

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished

    @finished.setter
    def finished(self, value: bool):
        with self._lock:
            self._finished = value

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @error.setter
    def error(self, value: str | None):
        with self._lock:
            self._error = value

    def start_run(self):
        with self._lock:
            self._started_at = time.time()
            self._steps.clear()
            self._finished = False
            self._error = None

    def add_step(self, name: str) -> StepStats:
        step = StepStats(name=name)
        with self._lock:
            self._steps.append(step)
        return step

    def mark_running(self, step: StepStats):
        with self._lock:
            step.status = "running"
            step.started_at = time.time()

    def mark_ok(self, step: StepStats):
        with self._lock:
            step.status = "ok"
            step.finished_at = time.time()

    def mark_failed(self, step: StepStats, error: str):
        with self._lock:
            step.status = "failed"
            step.finished_at = time.time()
            step.error = str(error)

    def mark_skipped(self, step: StepStats):
        with self._lock:
            step.status = "skipped"
            step.finished_at = time.time()

    @property
    def progress(self) -> float:
        with self._lock:
            if not self._steps:
                return 0.0
            completed = sum(1 for s in self._steps if s.status in ("ok", "failed", "skipped"))
            return round(completed / len(self._steps) * 100, 1)

    @property
    def results(self) -> dict:
        with self._lock:
            total = len(self._steps)
            ok = sum(1 for s in self._steps if s.status == "ok")
            failed = sum(1 for s in self._steps if s.status == "failed")
            skipped = sum(1 for s in self._steps if s.status == "skipped")
            elapsed = round(time.time() - self._started_at, 2) if self._started_at else 0.0
            return {
                "running": not self._finished,
                "finished": self._finished,
                "error": self._error,
                "total": total,
                "ok": ok,
                "failed": failed,
                "skipped": skipped,
                "progress_pct": self.progress,
                "elapsed_seconds": elapsed,
                "steps": [
                    {
                        "name": s.name,
                        "status": s.status,
                        "duration": s.duration,
                        "error": s.error,
                    }
                    for s in self._steps
                ],
            }

    def save_report(self, name: str = "report") -> str:
        path = Path(REPORT_DIR) / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.results, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)
