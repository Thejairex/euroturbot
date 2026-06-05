from playwright.sync_api import Page

from core.stats import StatsTracker
from core.exceptions import LoginError


def do_login(page: Page, stats: StatsTracker, url: str = "", username: str = "", password: str = "") -> None:
    from config.settings import ENV
    url = url or ENV["URL"]
    username = username or ENV["USERNAME"]
    password = password or ENV["PASSWORD"]
    step = stats.add_step("login")
    stats.mark_running(step)
    try:
        page.goto(url)
        page.wait_for_load_state("networkidle")

        page.fill("input[name='username']", username)
        page.fill("input[name='password']", password)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")

        if page.locator(".error-message, .alert-danger").is_visible():
            raise LoginError("Credenciales inválidas o error en login")

        stats.mark_ok(step)
    except Exception as e:
        stats.mark_failed(step, str(e))
        raise LoginError(f"Error en login: {e}") from e
