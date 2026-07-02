"""Memoria conversazionale leggera: l'ultimo turno riuscito per canale.

Serve ai follow-up ("accendi la lampada" → "ora spegnila"): l'ultimo
messaggio utente e il comando scelto vengono reiniettati nel prompt come una
coppia di messaggi reali, poche decine di token. Un solo turno, niente
storia lunga: su un modello 0.5B più contesto significa più occasioni di
scopiazzare il turno precedente invece di interpretare quello nuovo.

Stesso pattern di ``PendingIntentStore``: in-memoria, lock, TTL applicato
pigramente (niente thread di pulizia), chiave ``(channel, channel_key)`` —
una voce per chat/sessione, quindi memoria intrinsecamente limitata.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass


@dataclass
class LastTurn:
    user_text: str
    command: str
    arg: str | None
    created_at: float


class ConversationContextStore:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else float(os.getenv("AGENT_CONTEXT_TTL_SEC", "600"))
        )
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], LastTurn] = {}

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, channel: str, channel_key: str) -> LastTurn | None:
        if not self.enabled:
            return None
        with self._lock:
            self._purge()
            return self._items.get((channel, channel_key))

    def set(
        self, channel: str, channel_key: str, user_text: str, command: str, arg: str | None
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._purge()
            self._items[(channel, channel_key)] = LastTurn(
                user_text=user_text,
                command=command,
                arg=arg,
                created_at=time.monotonic(),
            )

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [key for key, item in self._items.items() if now - item.created_at > self._ttl]
        for key in expired:
            del self._items[key]
