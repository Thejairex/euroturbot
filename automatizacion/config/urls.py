from config.settings import ENV

URLS = {
    "base": ENV["URL"],
    "login": "",
    "dashboard": "",
}


def set_base_url(url: str) -> None:
    URLS["base"] = url.rstrip("/")


def build(path_key: str, **kwargs) -> str:
    base = URLS["base"]
    path = URLS.get(path_key, "")
    return f"{base}{path}".format(**kwargs)
