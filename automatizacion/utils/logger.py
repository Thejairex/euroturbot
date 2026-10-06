import logging
import sys
from pathlib import Path

from config.settings import LOG_DIR


def setup_logger(name: str = "automation", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # errors="backslashreplace": la consola de Windows usa el codepage ANSI (cp1252) por
    # default, que no puede representar algunos caracteres que aparecen en excepciones de
    # Playwright (ej. iconos de FontAwesome capturados en un snapshot de accesibilidad,
    # ) — confirmado en vivo (2026-09-30): un UnicodeEncodeError ahí tapaba el log
    # de error real (mensaje nunca llegaba a escribirse). Con esto se reemplaza el
    # carácter problemático en vez de reventar todo el logging.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:
        pass
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


log = setup_logger()
