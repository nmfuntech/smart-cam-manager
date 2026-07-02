"""Fast-path deterministico: frasi frequenti risolte senza chiamare l'LLM.

Su CPU limitata ogni chiamata Ollama costa secondi di prefill: le richieste
più comuni ("stato", "accendi la lampada", "gira a destra") hanno una forma
così prevedibile che un match deterministico è più affidabile del modello e
ha latenza ~zero.

Regole volutamente conservative — al minimo dubbio si ritorna ``None`` e
decide l'LLM:

- keyword: solo sul messaggio *intero* normalizzato, mai su sottostringhe;
- accendi/spegni: solo se il resto della frase risolve in modo univoco a un
  device noto (``resolve_name``);
- PTZ: solo verbo di movimento + esattamente una direzione.

L'output ha la stessa forma del JSON del modello (``{"command", "arg"}``) e
viene ripassato dallo stesso ``_validate_response`` di ``intent``: il
fast-path non può aggirare whitelist, ``validate_arg`` né il gate di
conferma per i comandi mutanti.
"""

from __future__ import annotations

import logging
from typing import Any

from blackframe.commands.naming import normalize_identifier, resolve_name

logger = logging.getLogger(__name__)

# Messaggio intero normalizzato (slug) -> comando. Niente prefissi/substring:
# "stato della lampada" NON deve matchare "stato".
_KEYWORDS: dict[str, str] = {
    "stato": "status",
    "status": "status",
    "come_va": "status",
    "come_stai": "status",
    "dispositivi": "devices",
    "elenca_i_dispositivi": "devices",
    "lista_dispositivi": "devices",
    "regole": "rules",
    "elenca_le_regole": "rules",
    "lista_regole": "rules",
    "eventi": "events",
    "ultimi_eventi": "events",
    "impostazioni": "config",
    "configurazione": "config",
    "foto": "snapshot",
    "scatta_una_foto": "snapshot",
    "fammi_una_foto": "snapshot",
    "snapshot": "snapshot",
    "ultimo_evento": "latest",
}

# Prefissi (sul messaggio normalizzato) per accensione/spegnimento device.
# L'ordine conta: i prefissi più lunghi vanno provati per primi, altrimenti
# "disattiva_" verrebbe mangiato da "attiva_".
_DEVICE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("disattiva_", "device_off"),
    ("spegni_", "device_off"),
    ("accendi_", "device_on"),
    ("attiva_", "device_on"),
)

_PTZ_VERBS = {"gira", "muovi", "sposta", "ruota", "inquadra", "punta"}
_PTZ_DIRECTIONS: dict[str, str] = {
    "sinistra": "ptz_left",
    "destra": "ptz_right",
    "alto": "ptz_up",
    "su": "ptz_up",
    "basso": "ptz_down",
    "giu": "ptz_down",
}


def _device_names(services: Any) -> list[str]:
    registry = getattr(services, "automation_registry", None) if services is not None else None
    if registry is None:
        return []
    try:
        return registry.device_names()
    except Exception:
        logger.exception("Fast-path: impossibile leggere i nomi device")
        return []


def _match_device_action(normalized: str, services: Any) -> dict | None:
    for prefix, command in _DEVICE_PREFIXES:
        if not normalized.startswith(prefix):
            continue
        rest = normalized[len(prefix) :]
        if not rest:
            return None
        candidates = _device_names(services)
        resolved, _ = resolve_name(rest, candidates)
        if resolved is not None:
            return {"command": command, "arg": resolved}
        # Prefisso giusto ma nessun device univoco: potrebbe essere un toggle
        # ("attiva il rilevamento movimento") — decide l'LLM.
        return None
    return None


def _match_ptz(normalized: str) -> dict | None:
    tokens = set(normalized.split("_"))
    if not tokens & _PTZ_VERBS:
        return None
    directions = [cmd for word, cmd in _PTZ_DIRECTIONS.items() if word in tokens]
    if len(directions) != 1:
        return None
    return {"command": directions[0], "arg": None}


def match(text: str, exclude: frozenset[str] = frozenset(), services: Any = None) -> dict | None:
    """Ritorna una proposta ``{"command", "arg"}`` nella stessa forma
    dell'output LLM, o ``None`` se nessuna regola conservativa scatta."""
    normalized = normalize_identifier(text)
    if not normalized:
        return None

    command = _KEYWORDS.get(normalized)
    if command is not None:
        return {"command": command, "arg": None} if command not in exclude else None

    device = _match_device_action(normalized, services)
    if device is not None:
        return device if device["command"] not in exclude else None

    ptz = _match_ptz(normalized)
    if ptz is not None:
        return ptz if ptz["command"] not in exclude else None

    return None
