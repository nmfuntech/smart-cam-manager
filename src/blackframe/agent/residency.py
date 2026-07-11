"""Policy di residenza e preload non bloccante per modelli Ollama."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import threading
from functools import lru_cache
from typing import Callable

from blackframe.envutil import env_float

from . import ollama_client

logger = logging.getLogger(__name__)

_VALID_MODES = frozenset({"auto", "idle", "off", "resident"})


def _parse_macos_vm_stat(output: str) -> float:
    page_size_match = re.search(r"page size of (\d+) bytes", output)
    if page_size_match is None:
        return 0.0
    page_size = int(page_size_match.group(1))
    pages = 0
    for label in ("free", "inactive", "speculative", "purgeable"):
        match = re.search(rf"Pages {label}:\s+(\d+)\.", output)
        if match is not None:
            pages += int(match.group(1))
    return pages * page_size / (1024 * 1024)


def available_ram_mb() -> float:
    """RAM fisica disponibile, best-effort e senza dipendenze esterne."""
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong),
                ("avail_phys", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("avail_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.avail_phys / (1024 * 1024)
        return 0.0
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["vm_stat"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return _parse_macos_vm_stat(result.stdout)
        except (OSError, subprocess.SubprocessError):
            return 0.0
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return float(pages * page_size) / (1024 * 1024)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0.0


def choose_keep_alive(
    mode: str,
    configured: str,
    available_mb: float,
    min_free_mb: float,
    model_estimate_mb: float,
) -> str:
    """Decide residenza senza cambiare durata legacy nelle modalità passive."""
    normalized_mode = str(mode or "idle").strip().lower()
    if normalized_mode not in _VALID_MODES:
        normalized_mode = "idle"
    fallback = str(configured or "5m").strip() or "5m"
    if normalized_mode == "resident":
        return "-1"
    if normalized_mode != "auto":
        return fallback
    required = max(0.0, min_free_mb) + max(0.0, model_estimate_mb)
    return "-1" if available_mb >= required else fallback


@lru_cache(maxsize=1)
def effective_keep_alive() -> str:
    """Decisione stabile per processo: evita oscillazioni dopo caricamento RAM."""
    return choose_keep_alive(
        os.getenv("AGENT_RESIDENCY_MODE", "auto"),
        os.getenv("AGENT_OLLAMA_KEEP_ALIVE", "30m"),
        available_ram_mb(),
        env_float("AGENT_RESIDENT_MIN_FREE_RAM_MB", 2048, minimum=0),
        env_float("AGENT_MODEL_ESTIMATED_RAM_MB", 1536, minimum=0),
    )


def reset_residency_decision() -> None:
    effective_keep_alive.cache_clear()


class ModelResidencyManager:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        mode: str | None = None,
        configured_keep_alive: str | None = None,
        warmup: Callable = ollama_client.warmup,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.mode = mode
        self.configured_keep_alive = configured_keep_alive
        self._warmup = warmup
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def keep_alive(self) -> str:
        if self.mode is None and self.configured_keep_alive is None:
            return effective_keep_alive()
        return choose_keep_alive(
            self.mode or os.getenv("AGENT_RESIDENCY_MODE", "auto"),
            self.configured_keep_alive
            or os.getenv("AGENT_OLLAMA_KEEP_ALIVE", "30m"),
            available_ram_mb(),
            env_float("AGENT_RESIDENT_MIN_FREE_RAM_MB", 2048, minimum=0),
            env_float("AGENT_MODEL_ESTIMATED_RAM_MB", 1536, minimum=0),
        )

    def preload_async(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            thread = threading.Thread(
                target=self._preload,
                name="agent-model-preload",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True

    def _preload(self) -> None:
        try:
            self._warmup(self.base_url, self.model, keep_alive=self.keep_alive)
        except Exception:
            logger.exception("Preload modello Ollama fallito")
        finally:
            with self._lock:
                self._thread = None

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
