"""Process-wide logging configuration.

- Timestamps on every line (the old default format had none).
- Optional size-rotated log file (``LOG_FILE``), so long-running services do
  not depend on the service manager for rotation.
- A repeat filter: when the same warning fires over and over (Telegram polling
  during a network outage, RTSP reconnect loops) only the first few are kept
  per window, then a single summary line reports how many were suppressed.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


class RepeatFilter(logging.Filter):
    """Let through at most ``burst`` identical records per ``window_sec``.

    Records are "identical" when logger name, level and message template match
    (arguments are ignored, so "fallito: timeout" and "fallito: DNS" from the
    same call site count together). Only WARNING and above are throttled.
    """

    def __init__(self, burst: int = 3, window_sec: float = 600.0) -> None:
        super().__init__()
        self.burst = max(1, burst)
        self.window_sec = max(1.0, window_sec)
        self._lock = threading.Lock()
        # key -> [window_start, count]
        self._seen: dict[tuple, list[float]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        key = (record.name, record.levelno, str(record.msg))
        now = time.monotonic()
        with self._lock:
            entry = self._seen.get(key)
            if entry is None or now - entry[0] >= self.window_sec:
                suppressed = int(entry[1]) - self.burst if entry else 0
                self._seen[key] = [now, 1]
                if len(self._seen) > 512:
                    self._prune(now)
                if suppressed > 0:
                    record.msg = f"{record.msg} (soppressi {suppressed} messaggi simili)"
                return True
            entry[1] += 1
            if entry[1] <= self.burst:
                return True
            if entry[1] == self.burst + 1:
                # One marker line so the reader knows the noise continues.
                record.msg = f"{record.msg} (ripetuto: messaggi simili soppressi per un po')"
                record.exc_info = None
                record.exc_text = None
                return True
            return False

    def _prune(self, now: float) -> None:
        stale = [k for k, v in self._seen.items() if now - v[0] >= self.window_sec]
        for k in stale:
            self._seen.pop(k, None)


def configure_logging(force: bool = False) -> None:
    """Configure the root logger once. Safe to call from every entrypoint."""
    global _configured
    if _configured and not force:
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    repeat_filter = RepeatFilter(
        burst=int(os.getenv("LOG_REPEAT_BURST", "3") or 3),
        window_sec=float(os.getenv("LOG_REPEAT_WINDOW_SEC", "600") or 600),
    )

    # Under the Windows service, stdout/stderr are files encoded as cp1252:
    # an emoji in a message must degrade to an escape, not a logging error.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(repeat_filter)
    root.addHandler(console)

    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        max_mb = float(os.getenv("LOG_FILE_MAX_MB", "10") or 10)
        backups = int(os.getenv("LOG_FILE_BACKUPS", "5") or 5)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max(1, int(max_mb * 1024 * 1024)),
            backupCount=max(1, backups),
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(repeat_filter)
        root.addHandler(file_handler)
        # The service manager still captures stdout/stderr: keep it for
        # warnings and crashes only, the full log lives in LOG_FILE.
        console.setLevel(logging.WARNING)

    # Chatty third-party loggers.
    for noisy in ("urllib3", "zeep", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
