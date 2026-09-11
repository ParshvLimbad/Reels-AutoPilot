"""Centralised logging for Reels-AutoPilot.

Provides a rotating file handler (5 MB x 3 backups) plus console output, with
timestamp, level, module and function name on every line.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import List, Optional

import config

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s.%(funcName)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def _resolve_level(level_name: Optional[str] = None) -> int:
    """Translate a configured level name into a logging level constant."""
    name = (level_name or getattr(config, "LOG_LEVEL", "INFO") or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def setup_logging(level_name: Optional[str] = None) -> logging.Logger:
    """Configure the root logger once, with rotation and console mirroring."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return root

    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=int(getattr(config, "LOG_MAX_BYTES", 5 * 1024 * 1024)),
        backupCount=int(getattr(config, "LOG_BACKUP_COUNT", 3)),
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(_resolve_level(level_name))
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # Third-party libraries are far too chatty on a Raspberry Pi.
    for noisy in ("moviepy", "urllib3", "werkzeug", "public_request", "private_request"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    setup_logging()
    return logging.getLogger(name)


def read_recent_logs(lines: Optional[int] = None) -> List[str]:
    """Return the last `lines` log lines (newest last) for the dashboard."""
    limit = int(lines or getattr(config, "DASHBOARD_LOG_LINES", 100))
    path = config.LOG_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-limit:]]
    except OSError as exc:  # pragma: no cover - defensive
        return [f"Could not read log file: {exc}"]
