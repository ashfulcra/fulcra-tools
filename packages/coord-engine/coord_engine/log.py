"""Structured logging for coord-reconcile.

Debugging instrumentation is built in from day one (levels + component + context),
not bolted on. Emits one JSON object per line to stderr so reconcile passes can be
traced (what entered/left each stage, why a file was skipped, timings). Stdlib-only.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


def _resolve_level(explicit: str | None) -> int:
    name = (explicit or os.environ.get("COORD_LOG_LEVEL") or "info").strip().lower()
    return _LEVELS.get(name, _LEVELS["info"])


class Logger:
    """Minimal structured logger: ``log.info("scanned", team=t, files=n)``."""

    def __init__(
        self, component: str, *, level: str | None = None, stream: TextIO | None = None
    ) -> None:
        self.component = component
        self.threshold = _resolve_level(level)
        self.stream = stream if stream is not None else sys.stderr

    def _emit(self, level: str, msg: str, **ctx: Any) -> None:
        if _LEVELS[level] < self.threshold:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": level,
            "component": self.component,
            "msg": msg,
        }
        record.update(ctx)
        try:
            self.stream.write(json.dumps(record, default=str) + "\n")
            self.stream.flush()
        except Exception:
            pass  # logging must never break the reconcile path

    def debug(self, msg: str, **ctx: Any) -> None:
        self._emit("debug", msg, **ctx)

    def info(self, msg: str, **ctx: Any) -> None:
        self._emit("info", msg, **ctx)

    def warn(self, msg: str, **ctx: Any) -> None:
        self._emit("warn", msg, **ctx)

    def error(self, msg: str, **ctx: Any) -> None:
        self._emit("error", msg, **ctx)


def get_logger(component: str, **kw: Any) -> Logger:
    return Logger(component, **kw)
