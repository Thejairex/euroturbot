import os
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from config.settings import SCREENSHOT_DIR, DEFAULT_TIMEOUT


class BrowserManager:
    def __init__(self, headless: bool = False):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.headless = headless
        self._screenshot_counter = 0
        self._is_running = False
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self, storage_state: str | None = None, init_script: str | None = None):
        self._is_running = True
        self._closed = False
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_opts = {"viewport": {"width": 1920, "height": 1080}, "locale": "es-ES"}
        if storage_state:
            ctx_opts["storage_state"] = storage_state
        self._context = self._browser.new_context(**ctx_opts)
        if init_script:
            self._context.add_init_script(init_script)
        self._page = self._context.new_page()
        self._page.set_default_timeout(DEFAULT_TIMEOUT)
        return self._page

    def save_session(self, store) -> None:
        store.save(self._context, self._page)

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    def screenshot(self, name: str = None) -> str:
        self._screenshot_counter += 1
        filename = f"{name or 'screenshot'}_{self._screenshot_counter}.png"
        path = str(SCREENSHOT_DIR / filename)
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        self.page.screenshot(path=path, full_page=True)
        return path

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._is_running = False
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except Exception:
                pass

    # Alias semántico: se llama desde otro thread para desbloquear al worker Playwright
    force_close = close
