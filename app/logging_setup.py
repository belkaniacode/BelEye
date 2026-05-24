from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from .paths import log_file


def setup_logging(level: str | int | None = None) -> None:
    env_level = os.getenv("BELEYE_LOG_LEVEL", "INFO")
    effective = level if level is not None else env_level

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(effective)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            log_file(),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("Could not initialize file logging: %s", exc)
