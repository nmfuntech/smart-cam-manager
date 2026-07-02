"""Normalizzazione e risoluzione tollerante dei nomi device/regola.

L'agente LLM e i comandi digitati devono poter riferirsi a un device come
``lampada_ingresso`` scrivendo "la lampada dell'ingresso": qui si normalizza
la stringa grezza a uno slug e, se possibile, la si risolve contro l'elenco
reale dei nomi noti. La risoluzione automatica avviene solo su match
univoco: in caso di ambiguità o assenza di candidati plausibili non si
sceglie a caso, per non rischiare di azionare il device sbagliato.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import get_close_matches

_STOPWORDS = {
    "il",
    "lo",
    "la",
    "i",
    "gli",
    "le",
    "un",
    "uno",
    "una",
    "di",
    "del",
    "della",
    "dello",
    "dei",
    "degli",
    "delle",
    "a",
    "al",
    "allo",
    "alla",
    "ai",
    "agli",
    "alle",
    "in",
    "nel",
    "nello",
    "nella",
    "nei",
    "negli",
    "nelle",
    "l",
}


def normalize_identifier(text: str) -> str:
    """Converte testo libero in uno slug ``[a-z0-9_]+`` (accenti/spazi/punteggiatura)."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def _tokens(identifier: str) -> set[str]:
    return {t for t in identifier.split("_") if t and t not in _STOPWORDS}


def resolve_name(raw: str, candidates: list[str], limit: int = 3) -> tuple[str | None, list[str]]:
    """Risolve ``raw`` (già uno slug) contro ``candidates``.

    Ritorna ``(nome_risolto, suggerimenti)``: il nome è popolato solo su
    match univoco (esatto, per sovrapposizione di token ignorando le
    preposizioni italiane, o per similarità di stringa); altrimenti è
    ``None`` e ``suggerimenti`` contiene fino a ``limit`` candidati papabili.
    """
    if not raw or not candidates:
        return None, []
    if raw in candidates:
        return raw, []

    raw_tokens = _tokens(raw)
    token_matches = []
    if raw_tokens:
        token_matches = [
            c for c in candidates if raw_tokens <= _tokens(c) or _tokens(c) <= raw_tokens
        ]
        if len(token_matches) == 1:
            return token_matches[0], []

    close = get_close_matches(raw, candidates, n=limit, cutoff=0.6)
    if len(close) == 1:
        return close[0], []

    suggestions = close or token_matches
    return None, suggestions[:limit]
