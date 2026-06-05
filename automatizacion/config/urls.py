from config.settings import ENV

URLS = {
    "base": ENV["URL"],
    "login": ENV["LOGIN_PATH"],
    "creditor": ENV["CREDITOR_PATH"],
}


def set_base_url(url: str) -> None:
    URLS["base"] = url.rstrip("/")


def build(path_key: str) -> str:
    base = URLS["base"]
    path = URLS.get(path_key, "")
    return f"{base}{path}"


def spa_url(path_key: str) -> str:
    base = URLS["base"]
    path = URLS.get(path_key, "")
    return f"{base}{path}"
