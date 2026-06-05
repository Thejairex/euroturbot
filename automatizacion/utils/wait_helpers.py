import time

from playwright.sync_api import Page

from config.settings import DEFAULT_TIMEOUT, POLLING_INTERVAL


def wait_for_stable_dom(page: Page, timeout: int = DEFAULT_TIMEOUT) -> None:
    """Wait until DOM stops mutating (useful for SPA with dynamic content)."""
    elapsed = 0
    last_html = page.content()
    while elapsed < timeout:
        time.sleep(POLLING_INTERVAL / 1000)
        elapsed += POLLING_INTERVAL
        current_html = page.content()
        if current_html == last_html:
            return
        last_html = current_html


def wait_for_element_stable(page: Page, selector: str, timeout: int = DEFAULT_TIMEOUT) -> None:
    """Wait for a specific element to stop changing."""
    elapsed = 0
    last_text = ""
    while elapsed < timeout:
        try:
            current_text = page.locator(selector).inner_text()
            if current_text and current_text != "" and current_text == last_text:
                return
            last_text = current_text
        except Exception:
            pass
        time.sleep(POLLING_INTERVAL / 1000)
        elapsed += POLLING_INTERVAL


def retry(fn, retries: int = 3, delay: float = 1.0):
    """Generic retry helper for flaky operations."""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
    raise last_exc
