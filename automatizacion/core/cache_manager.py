import json
import time
import os
from pathlib import Path

from config.settings import CACHE_TTL_SECONDS, CACHE_DISK_ENABLED, CACHE_DISK_DIR


class CacheManager:
    def __init__(self):
        self._memory: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        if key in self._memory:
            ts, value = self._memory[key]
            if time.time() - ts < CACHE_TTL_SECONDS:
                return value
            del self._memory[key]

        if CACHE_DISK_ENABLED:
            value = self._read_disk(key)
            if value is not None:
                self._memory[key] = (time.time(), value)
                return value
        return None

    def set(self, key: str, value: object) -> None:
        self._memory[key] = (time.time(), value)
        if CACHE_DISK_ENABLED:
            self._write_disk(key, value)

    def invalidate(self, key: str) -> None:
        self._memory.pop(key, None)
        if CACHE_DISK_ENABLED:
            path = self._disk_path(key)
            if path.exists():
                path.unlink()

    def clear(self) -> None:
        self._memory.clear()
        if CACHE_DISK_ENABLED and CACHE_DISK_DIR.exists():
            for f in CACHE_DISK_DIR.iterdir():
                f.unlink()

    def _disk_path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in key)
        return CACHE_DISK_DIR / f"{safe}.json"

    def _read_disk(self, key: str) -> object | None:
        path = self._disk_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data["ts"] < CACHE_TTL_SECONDS:
                return data["value"]
        except Exception:
            pass
        return None

    def _write_disk(self, key: str, value: object) -> None:
        try:
            os.makedirs(CACHE_DISK_DIR, exist_ok=True)
            data = json.dumps({"ts": time.time(), "value": value}, ensure_ascii=False)
            self._disk_path(key).write_text(data, encoding="utf-8")
        except Exception:
            pass
