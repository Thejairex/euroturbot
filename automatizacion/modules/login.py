from playwright.sync_api import Page, expect

from core.stats import StatsTracker
from core.exceptions import LoginError
from config.urls import spa_url


def do_login(page: Page, stats: StatsTracker, url: str = "", username: str = "", password: str = "") -> None:
    from config.settings import ENV
    url = url or spa_url("login")
    username = username or ENV["USERNAME"]
    password = password or ENV["PASSWORD"]

    step = stats.add_step("login")
    stats.mark_running(step)
    try:
        page.goto(url)
        page.wait_for_load_state("networkidle")

        page.fill("input.username", username)
        page.fill("input.password", password)
        page.click("button.tpbutton.login")

        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_function(
            "!window.location.hash.includes('login')",
            timeout=15000,
        )

        stats.mark_ok(step)
    except Exception as e:
        stats.mark_failed(step, str(e))
        raise LoginError(f"Error en login: {e}") from e


def is_logged_in(page: Page) -> bool:
    """Navega a creditor y verifica si la sesión sigue activa (sin login)."""
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    return "login" not in page.url


def ensure_logged_in(page: Page, stats: StatsTracker) -> None:
    if not is_logged_in(page):
        do_login(page, stats)
