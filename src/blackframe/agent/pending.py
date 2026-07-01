"""Store in-memoria delle proposte dell'agente in attesa di conferma umana.

Nessun thread dedicato: la scadenza (TTL) viene applicata pigramente ad ogni
``create``/``pop``, stesso pattern di ``RateLimiter._evict`` in ``auth.py`` —
niente da avviare/fermare, coerente con un layer che deve restare leggero su
hardware limitato.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass


@dataclass
class PendingIntent:
    channel: str
    channel_key: str
    command: str
    arg: str | None
    created_at: float


class PendingIntentStore:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else float(os.getenv("AGENT_PENDING_TTL_SEC", "120"))
        )
        self._lock = threading.Lock()
        self._items: dict[str, PendingIntent] = {}

    def create(self, channel: str, channel_key: str, command: str, arg: str | None) -> str:
        pending_id = uuid.uuid4().hex
        with self._lock:
            self._purge()
            self._items[pending_id] = PendingIntent(
                channel=channel,
                channel_key=channel_key,
                command=command,
                arg=arg,
                created_at=time.monotonic(),
            )
        return pending_id

    def pop(self, pending_id: str, channel: str, channel_key: str) -> PendingIntent | None:
        """Consuma la proposta se esiste, non è scaduta e appartiene allo
        stesso canale/chiave (una chat/sessione non può confermare la
        proposta di un'altra)."""
        with self._lock:
            self._purge()
            item = self._items.get(pending_id)
            if item is None:
                return None
            if item.channel != channel or item.channel_key != channel_key:
                return None
            return self._items.pop(pending_id)

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [key for key, item in self._items.items() if now - item.created_at > self._ttl]
        for key in expired:
            del self._items[key]
