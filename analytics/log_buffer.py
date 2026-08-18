"""In-memory ring buffer of log records for the Live Logs page."""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_BUFFER: collections.deque[dict[str, Any]] = collections.deque(maxlen=1000)
_SEQ = 0
_SKIP_PATHS = ("/stats/", "/dashboard", "/docs", "/openapi", "/favicon", "/redoc")


class LogBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.name == "uvicorn.access" and any(p in msg for p in _SKIP_PATHS):
                return
            global _SEQ
            with _LOCK:
                _SEQ += 1
                seq = _SEQ
                _BUFFER.append({"seq": seq, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "level": record.levelname, "logger": record.name, "message": msg})
        except Exception:
            pass


def install(level: int = logging.INFO) -> None:
    h = LogBufferHandler()
    h.setLevel(level)
    logging.getLogger().addHandler(h)


def get_logs(since: int = 0, limit: int = 500) -> dict[str, Any]:
    with _LOCK:
        items = [dict(r) for r in _BUFFER if r["seq"] > since]
        last = _BUFFER[-1]["seq"] if _BUFFER else 0
    if len(items) > limit:
        items = items[-limit:]
    return {"items": items, "last_seq": last}
