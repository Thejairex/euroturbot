from playwright.sync_api import Page, expect

from config.settings import DEFAULT_TIMEOUT, POLLING_INTERVAL


def wait_for_spa_ready(page: Page, timeout: int = DEFAULT_TIMEOUT) -> None:
    page.wait_for_load_state("networkidle", timeout=timeout)
    page.wait_for_load_state("domcontentloaded", timeout=timeout)


def wait_and_click(page: Page, selector: str, timeout: int = DEFAULT_TIMEOUT) -> None:
    locator = page.locator(selector)
    expect(locator).to_be_visible(timeout=timeout)
    expect(locator).to_be_enabled(timeout=timeout)
    locator.click()


def wait_and_fill(page: Page, selector: str, value: str, timeout: int = DEFAULT_TIMEOUT) -> None:
    locator = page.locator(selector)
    expect(locator).to_be_visible(timeout=timeout)
    locator.fill(value)


def wait_for_table_data(page: Page, table_selector: str = "table", timeout: int = DEFAULT_TIMEOUT) -> None:
    locator = page.locator(table_selector)
    expect(locator).to_be_visible(timeout=timeout)
    page.wait_for_function(
        f"document.querySelector('{table_selector}')?.rows?.length > 0",
        timeout=timeout,
    )
