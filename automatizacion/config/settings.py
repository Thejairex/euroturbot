import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

ENV = {
    "USERNAME": os.getenv("LOGIN_USERNAME", ""),
    "PASSWORD": os.getenv("LOGIN_PASSWORD", ""),
    "URL": os.getenv("LOGIN_URL", ""),
}

DEFAULT_TIMEOUT = 30000
NAVIGATION_TIMEOUT = 60000
POLLING_INTERVAL = 500

CACHE_ENABLED = True
CACHE_TTL_SECONDS = 300
CACHE_DISK_ENABLED = True
CACHE_DISK_DIR = BASE_DIR / "outputs" / "cache"

HEADLESS_DEFAULT = False

SCREENSHOT_DIR = BASE_DIR / "outputs" / "screenshots"
LOG_DIR = BASE_DIR / "outputs" / "logs"
REPORT_DIR = BASE_DIR / "outputs" / "reports"

SCREENSHOT_ON_ERROR = True
MAX_RETRIES = 3
