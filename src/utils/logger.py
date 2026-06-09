"""Simple structured logger used throughout the harness."""

from __future__ import annotations

import json
import logging
import sys
from enum import Enum


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


_LEVEL_MAP: dict[LogLevel, int] = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARN: logging.WARN,
    LogLevel.ERROR: logging.ERROR,
}


def _handler() -> logging.StreamHandler:
    h = logging.StreamHandler(sys.stdout)

    def _fmt(record: logging.LogRecord) -> str:
        ts = _iso_now()
        meta = ""
        if hasattr(record, "meta") and record.meta:
            meta = " " + json.dumps(record.meta)
        return f"[{ts}] [{record.levelname}] {record.getMessage()}{meta}"

    h.setFormatter(logging.Formatter())
    h.format = _fmt  # type: ignore[method-assign]
    return h


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_log = logging.getLogger("harness")
_log.setLevel(logging.INFO)
_log.addHandler(_handler())
_log.propagate = False


def set_log_level(level: LogLevel | str) -> None:
    if isinstance(level, str):
        level = LogLevel(level.upper())
    _log.setLevel(_LEVEL_MAP[level])


# Public convenience wrapper so callers don't touch ``logging`` directly.


def debug(msg: str, **meta: object) -> None:
    _log.debug(msg, extra={"meta": meta})


def info(msg: str, **meta: object) -> None:
    _log.info(msg, extra={"meta": meta})


def warn(msg: str, **meta: object) -> None:
    _log.warning(msg, extra={"meta": meta})


def error(msg: str, **meta: object) -> None:
    _log.error(msg, extra={"meta": meta})