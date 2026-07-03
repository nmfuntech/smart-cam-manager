"""Letture tipizzate da variabili d'ambiente, in un posto solo.

Gli helper ``_env*`` erano reimplementati (con piccole varianti) in sei
moduli: qui vive la versione unica. Semantica comune: valore assente o non
parsabile -> default; ``minimum`` opzionale fa da pavimento per i numerici.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value
