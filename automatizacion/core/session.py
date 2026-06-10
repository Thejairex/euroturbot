import json
from pathlib import Path

from config.settings import BASE_DIR

SESSION_DIR = BASE_DIR / "outputs" / "session"
_STATE_FILE = SESSION_DIR / "storage_state.json"
_SESSION_STORAGE_FILE = SESSION_DIR / "session_storage.json"


class SessionStore:
    def exists(self) -> bool:
        return _STATE_FILE.exists()

    def state_path(self) -> str | None:
        return str(_STATE_FILE) if _STATE_FILE.exists() else None

    def init_script(self) -> str | None:
        """Retorna un JS que repuebla sessionStorage antes de que Angular bootee."""
        if not _SESSION_STORAGE_FILE.exists():
            return None
        try:
            data = json.loads(_SESSION_STORAGE_FILE.read_text(encoding="utf-8"))
            if not data:
                return None
            entries = json.dumps(data)
            return f"""
(function() {{
    var data = {entries};
    for (var key in data) {{
        try {{ sessionStorage.setItem(key, data[key]); }} catch(e) {{}}
    }}
}})();
"""
        except Exception:
            return None

    def save(self, context, page) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(_STATE_FILE))
        try:
            ss_raw = page.evaluate("JSON.stringify(Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)])))")
            ss_data = json.loads(ss_raw) if ss_raw else {}
            _SESSION_STORAGE_FILE.write_text(json.dumps(ss_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            _SESSION_STORAGE_FILE.write_text("{}", encoding="utf-8")

    def clear(self) -> None:
        for f in (_STATE_FILE, _SESSION_STORAGE_FILE):
            if f.exists():
                f.unlink()
