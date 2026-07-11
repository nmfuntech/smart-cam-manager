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

from blackframe.capabilities import build_services_registry
from blackframe.commands.naming import normalize_identifier, resolve_name

from .entities import resolve_entity

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
    "riepilogo_impostazioni": "config",
    "che_configurazione_hai_adesso": "config",
    "fammi_vedere_l_ultimo_evento": "latest",
    "cosa_e_successo_oggi": "events",
    "quali_dispositivi_ci_sono": "devices",
    "le_luci_sono_accese": "devices",
    "che_regole_di_automazione_ci_sono": "rules",
    "mandami_uno_scatto_della_camera": "snapshot",
    "come_sta_la_telecamera": "status",
    "com_e_la_situazione": "status",
}

_EXACT_ACTIONS: dict[str, tuple[str, str | None]] = {
    "attiva_il_rilevamento_movimento": ("motion_on", None),
    "spegni_il_movimento": ("motion_off", None),
    "attiva_il_riconoscimento": ("classification_on", None),
    "disattiva_il_riconoscimento_di_persone_e_animali": ("classification_off", None),
    "avvisami_quando_vedi_una_persona": ("detect_person_on", None),
    "ignora_le_persone": ("detect_person_off", None),
    "avvisami_se_c_e_il_cane": ("detect_pet_on", None),
    "ignora_gli_animali": ("detect_pet_off", None),
    "attiva_le_notifiche": ("notifications_on", None),
    "spegni_le_notifiche": ("notifications_off", None),
    "riattiva_le_notifiche": ("resume", None),
    "zitto_per_un_po": ("mute", None),
    "attiva_le_clip_video_degli_eventi": ("record_on", None),
    "niente_piu_clip_per_gli_eventi": ("record_off", None),
    "attiva_la_registrazione_continua": ("continuous_on", None),
    "ferma_la_registrazione_continua": ("continuous_off", None),
    "guarda_in_alto": ("ptz_up", None),
    "inquadra_piu_in_basso": ("ptz_down", None),
    "ferma_il_movimento_della_telecamera": ("ptz_stop", None),
    "torna_in_posizione_iniziale": ("ptz_home", None),
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

_INVENTORY_QUERY_WORDS = {"che", "elenca", "fammi", "mostra", "mostrami", "quali", "vedere"}
_INVENTORY_ENTITY_WORDS = {
    "apparecchi",
    "device",
    "dispositivi",
    "entita",
    "inventario",
}


def _match_inventory(normalized: str) -> dict | None:
    """Riconosce richieste d'inventario senza enumerare ogni frase possibile."""
    tokens = set(normalized.split("_"))
    if tokens & _INVENTORY_QUERY_WORDS and tokens & _INVENTORY_ENTITY_WORDS:
        return {"command": "inventory", "arg": None}
    return None


def _match_entity_status(normalized: str, services: Any) -> dict | None:
    tokens = set(normalized.split("_"))
    state_tokens = {"accesa", "acceso", "spenta", "spento", "sta", "stato", "status"}
    if not tokens & state_tokens or services is None:
        return None
    inventory = build_services_registry(services).snapshot()
    resolution = resolve_entity(
        normalized,
        inventory.entities,
        capability_id="state.read",
    )
    if resolution.entity is None:
        return None
    return {"command": "entity_status", "arg": resolution.entity.name}


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


def _match_sensitivity(normalized: str) -> dict | None:
    if "sensibilita" not in normalized:
        return None
    if any(word in normalized.split("_") for word in ("alta", "aumenta", "alza")):
        return {"command": "sensitivity", "arg": "alta"}
    if any(word in normalized.split("_") for word in ("bassa", "abbassa", "riduci")):
        return {"command": "sensitivity", "arg": "bassa"}
    if "media" in normalized.split("_"):
        return {"command": "sensitivity", "arg": "media"}
    return None


def _match_mute(normalized: str) -> dict | None:
    tokens = normalized.split("_")
    if not ({"silenzia", "muto", "zitto"} & set(tokens)):
        return None
    number = next((token for token in tokens if token.isdigit()), None)
    if number is None:
        return {"command": "mute", "arg": None}
    # Registry mute argument is expressed in minutes. Bare numbers keep same unit.
    value = float(number)
    if any(token.startswith("second") for token in tokens):
        value /= 60
    elif "ore" in tokens:
        value *= 60
    rendered = str(int(value)) if value.is_integer() else str(value)
    return {"command": "mute", "arg": rendered}


def _match_rule_action(normalized: str) -> dict | None:
    prefixes = (
        ("esegui_la_regola_", "rule_run"),
        ("avvia_la_regola_", "rule_run"),
        ("abilita_la_regola_", "rule_on"),
        ("disabilita_la_regola_", "rule_off"),
    )
    for prefix, command in prefixes:
        if normalized.startswith(prefix) and normalized[len(prefix) :]:
            return {"command": command, "arg": normalized[len(prefix) :]}
    return None


def _match_followup(normalized: str, last_turn: Any) -> dict | None:
    if last_turn is None:
        return None
    previous = getattr(last_turn, "command", None)
    arg = getattr(last_turn, "arg", None)
    if normalized in {"ora_spegnila", "spegnila"} and previous in {"device_on", "device_off"}:
        return {"command": "device_off", "arg": arg}
    if normalized in {"accendila_di_nuovo", "riaccendila"} and previous in {
        "device_on",
        "device_off",
    }:
        return {"command": "device_on", "arg": arg}
    if normalized in {"e_ora_a_destra", "ora_a_destra"} and str(previous).startswith("ptz_"):
        return {"command": "ptz_right", "arg": None}
    if normalized == "riattivalo":
        inverse = {
            "motion_off": "motion_on",
            "classification_off": "classification_on",
            "record_off": "record_on",
            "continuous_off": "continuous_on",
        }
        if previous in inverse:
            return {"command": inverse[previous], "arg": None}
    if normalized == "anzi_no_riaccendile" and previous in {"notifications_off", "resume"}:
        return {"command": "notifications_on", "arg": None}
    return None


def match(
    text: str,
    exclude: frozenset[str] = frozenset(),
    services: Any = None,
    last_turn: Any = None,
) -> dict | None:
    """Ritorna una proposta ``{"command", "arg"}`` nella stessa forma
    dell'output LLM, o ``None`` se nessuna regola conservativa scatta."""
    normalized = normalize_identifier(text)
    if not normalized:
        return None

    followup = _match_followup(normalized, last_turn)
    if followup is not None:
        return followup if followup["command"] not in exclude else None

    command = _KEYWORDS.get(normalized)
    if command is not None:
        return {"command": command, "arg": None} if command not in exclude else None

    exact = _EXACT_ACTIONS.get(normalized)
    if exact is not None:
        command, arg = exact
        return {"command": command, "arg": arg} if command not in exclude else None

    inventory = _match_inventory(normalized)
    if inventory is not None:
        return inventory if inventory["command"] not in exclude else None

    entity_status = _match_entity_status(normalized, services)
    if entity_status is not None:
        return entity_status if entity_status["command"] not in exclude else None

    sensitivity = _match_sensitivity(normalized)
    if sensitivity is not None:
        return sensitivity if sensitivity["command"] not in exclude else None

    mute = _match_mute(normalized)
    if mute is not None:
        return mute if mute["command"] not in exclude else None

    rule = _match_rule_action(normalized)
    if rule is not None:
        return rule if rule["command"] not in exclude else None

    device = _match_device_action(normalized, services)
    if device is not None:
        return device if device["command"] not in exclude else None

    ptz = _match_ptz(normalized)
    if ptz is not None:
        return ptz if ptz["command"] not in exclude else None

    return None
