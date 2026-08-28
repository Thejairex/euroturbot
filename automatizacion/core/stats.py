import time
import json
import logging
from collections import deque
from threading import RLock
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
        self._lock = RLock()
        self._finished = False
        self._error: str | None = None
        self._last_activity: float | None = None
        self._activity: dict = {}
        self._events: deque = deque(maxlen=200)
        self._event_seq: int = 0
        self._skipped: list[dict] = []
        self._vouchers: deque = deque(maxlen=1000)
        self._voucher_seq: int = 0
        self._total_planned: int | None = None

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
            self._last_activity = time.time()
            self._activity = {}
            self._events.clear()
            self._event_seq = 0
            self._skipped = []
            self._vouchers.clear()
            self._voucher_seq = 0
            self._total_planned = None

    def add_step(self, name: str) -> StepStats:
        step = StepStats(name=name)
        with self._lock:
            self._steps.append(step)
        return step

    def set_total_planned(self, n: int) -> None:
        """Total real de proveedores a procesar en esta corrida. `add_step` se llama de a
        uno a medida que el loop los alcanza, así que `len(self._steps)` NO refleja el
        total hasta que la corrida termine — sin esto, un reporte a mitad de una corrida
        larga muestra "1/2 proveedores" cuando en realidad son "1/302" (confuso: parece que
        la corrida entera tenía solo 2 proveedores, no que se cortó a mitad de 302)."""
        with self._lock:
            self._total_planned = n

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

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.time()

    def set_activity(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                if v is None:
                    self._activity.pop(k, None)
                else:
                    self._activity[k] = v
            self._last_activity = time.time()

    def clear_activity(self) -> None:
        with self._lock:
            self._activity = {}

    def add_event(self, level: str, message: str) -> None:
        with self._lock:
            self._event_seq += 1
            self._events.append({
                "seq": self._event_seq,
                "ts": time.strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            })
            self._last_activity = time.time()

    def add_skipped(self, entry: dict) -> None:
        with self._lock:
            self._skipped.append(entry)

    def events_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def add_voucher_result(self, entry: dict) -> None:
        with self._lock:
            self._voucher_seq += 1
            self._vouchers.append({**entry, "seq": self._voucher_seq, "ts": time.strftime("%H:%M:%S")})

    def vouchers_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [v for v in self._vouchers if v["seq"] > seq]

    @property
    def last_activity_age(self) -> float | None:
        with self._lock:
            if self._last_activity is None:
                return None
            return round(time.time() - self._last_activity, 1)

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
            last_age = None
            if self._last_activity is not None:
                last_age = round(time.time() - self._last_activity, 1)
            return {
                "running": self._started_at is not None and not self._finished,
                "finished": self._finished,
                "error": self._error,
                "total": total,
                "total_planned": self._total_planned,
                "ok": ok,
                "failed": failed,
                "skipped": skipped,
                "progress_pct": self.progress,
                "elapsed_seconds": elapsed,
                "last_activity_age": last_age,
                "activity": dict(self._activity),
                "skipped_vouchers": list(self._skipped),
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

    def save_summary_report(self, name: str = "resumen_ejecucion", tracker=None) -> str:
        """Reporte LEGIBLE (Markdown) de la corrida: qué hizo, qué falló, en qué se quedó
        si se cortó a mitad, y el estado acumulado del tracker (todas las corridas, no
        solo esta). Pensado para revisar sin tener que consultar el log crudo.

        Se llama desde _cleanup/_finish en main.py — corre en todos los caminos de
        salida que pasan por código Python (fin normal, Ctrl+C, excepción, force-stop
        del monitor). Un kill duro del proceso (taskkill /F, cierre de la ventana,
        corte de luz) no puede interceptarse en ningún lenguaje — en ese caso no hay
        reporte de esta corrida, solo el de la corrida anterior que sí cerró bien."""
        r = self.results
        lines = [
            f"# Resumen de ejecución — {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"**Estado final:** {'ERROR: ' + r['error'] if r['error'] else ('OK' if r['finished'] else 'INTERRUMPIDO')}",
            f"**Duración:** {r['elapsed_seconds']:.0f}s",
            (f"**Progreso:** proveedor {r['total']}/{r['total_planned']} de la corrida "
             f"({r['ok']} terminado(s) sin excepción, {r['failed']} con excepción, {r['skipped']} salteado(s))"
             if r.get("total_planned") else
             f"**Progreso:** {r['progress_pct']}% ({r['ok'] + r['failed'] + r['skipped']}/{r['total']} proveedores)"),
            "",
            "## Esta corrida",
            f"- OK: {r['ok']}",
            f"- Failed: {r['failed']}",
            f"- Skipped: {r['skipped']}",
        ]

        if r["activity"]:
            lines += ["", "## Última actividad conocida",
                      "(dónde se quedó si la corrida se cortó a mitad de un proveedor)", ""]
            lines += [f"- {k}: {v}" for k, v in r["activity"].items()]

        fallidos = [s for s in r["steps"] if s["status"] == "failed"]
        if fallidos:
            lines += ["", "## Proveedores con error en esta corrida", ""]
            lines += [f"- {s['name']}: {s['error']} ({s['duration']}s)" for s in fallidos]

        if tracker is not None:
            try:
                counts = tracker.get_status_counts()
                lines += ["", "## Estado acumulado del tracker (todos los archivos)", ""]
                for status in ("ok", "pending", "failed", "skipped", "disabled", "processing"):
                    if status in counts:
                        lines.append(f"- {status}: {counts[status]}")
            except Exception as e:
                lines += ["", f"(No se pudo leer el estado del tracker: {e})"]

        eventos = list(self._events)[-30:]
        if eventos:
            lines += ["", "## Últimos eventos del log", ""]
            lines += [f"- [{e['ts']}] {e['level']}: {e['message']}" for e in eventos]

        path = Path(REPORT_DIR) / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)


class StatsEventHandler(logging.Handler):
    """Logging handler que alimenta los eventos del StatsTracker en curso."""

    def __init__(self, get_stats):
        super().__init__(level=logging.INFO)
        self._get_stats = get_stats

    def emit(self, record):
        try:
            s = self._get_stats()
            if s is not None:
                s.add_event(record.levelname, record.getMessage())
        except Exception:
            pass
