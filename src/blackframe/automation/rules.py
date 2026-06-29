"""Caricamento e validazione delle regole di automazione (YAML).

Le regole sono la parte config-driven del layer: definiscono `evento → condizione
→ azione` senza toccare il codice. Questo modulo le legge da YAML, le valida una
volta sola all'avvio (così un errore di battitura emerge subito, non a runtime in
mezzo a un evento) e le espone come dataclass immutabili che l'engine consuma.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .events import CATEGORY_EVENT_MAP

# Eventi ammessi nel campo `on` di una regola: gli stessi che l'engine deriva da
# EventContext.event_name. Tenere questa lista allineata a CATEGORY_EVENT_MAP.
VALID_EVENTS = frozenset(CATEGORY_EVENT_MAP.values())

# Azioni ammesse su un device. set_state richiede un payload `state`.
VALID_ACTIONS = frozenset({"turn_on", "turn_off", "set_state"})

_DURATION_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)
_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}


class RuleConfigError(ValueError):
    """Errore nel file regole: sollevato in fase di load, mai durante un evento."""


def parse_duration(value) -> float:
    """Converte '120s' / '5m' / '1h' / '90' in secondi. Numeri nudi = secondi."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_PATTERN.match(str(value))
    if not match:
        raise RuleConfigError(f"Durata non valida: '{value}' (usa es. 120s, 5m, 1h)")
    amount, unit = match.group(1), match.group(2).lower()
    return float(amount) * _UNIT_SECONDS[unit]


def _parse_hhmm(value: str) -> int:
    """Converte 'HH:MM' nel minuto del giorno [0, 1440)."""
    match = _TIME_PATTERN.match(str(value).strip())
    if not match:
        raise RuleConfigError(f"Orario non valido: '{value}' (atteso HH:MM 24h)")
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_time_window(value) -> tuple[int, int] | None:
    """Converte ['18:00','07:00'] in (start_min, end_min). Supporta wrap notturno."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RuleConfigError(
            "Finestra oraria: attesa lista [inizio, fine] (es. ['18:00','07:00'])"
        )
    return _parse_hhmm(value[0]), _parse_hhmm(value[1])


def minute_in_window(minute: int, window: tuple[int, int]) -> bool:
    """True se `minute` ricade nella finestra. start==end = sempre vero (24h)."""
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    # Wrap a mezzanotte (es. 18:00 → 07:00).
    return minute >= start or minute < end


@dataclass(frozen=True)
class Action:
    """Una singola azione su un device dentro una regola."""

    device: str
    action: str
    state: dict | None = None
    for_seconds: float = 0.0  # auto-off opzionale: 0 = nessuno


@dataclass(frozen=True)
class Rule:
    """Una regola di automazione: evento + condizioni + azioni + cooldown."""

    name: str
    event: str
    actions: tuple[Action, ...]
    source: str | None = None
    window: tuple[int, int] | None = None
    cooldown_seconds: float = 0.0
    devices: frozenset[str] = field(default_factory=frozenset)


def _parse_action(raw: dict, rule_name: str) -> Action:
    if not isinstance(raw, dict):
        raise RuleConfigError(f"Regola '{rule_name}': ogni voce di 'do' deve essere un dict")
    device = str(raw.get("device") or "").strip()
    if not device:
        raise RuleConfigError(f"Regola '{rule_name}': azione senza 'device'")
    action = str(raw.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        raise RuleConfigError(
            f"Regola '{rule_name}': azione '{action}' non valida "
            f"(ammesse: {', '.join(sorted(VALID_ACTIONS))})"
        )
    state = raw.get("state")
    if action == "set_state":
        if not isinstance(state, dict) or not state:
            raise RuleConfigError(
                f"Regola '{rule_name}': set_state richiede un 'state' (dict) non vuoto"
            )
    elif state is not None:
        raise RuleConfigError(f"Regola '{rule_name}': 'state' è ammesso solo con action set_state")
    return Action(
        device=device,
        action=action,
        state=dict(state) if isinstance(state, dict) else None,
        for_seconds=parse_duration(raw.get("for")),
    )


def _parse_rule(raw: dict, known_devices: set[str] | None) -> Rule:
    if not isinstance(raw, dict):
        raise RuleConfigError("Ogni regola deve essere un dict")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RuleConfigError("Regola senza 'name'")
    # YAML 1.1 interpreta la chiave non quotata `on` come booleano True: accettiamo
    # entrambe le forme così l'utente può scrivere `on: person_detected` naturalmente.
    raw_event = raw.get("on")
    if raw_event is None and True in raw:
        raw_event = raw[True]
    event = str(raw_event or "").strip()
    if event not in VALID_EVENTS:
        raise RuleConfigError(
            f"Regola '{name}': evento '{event}' non valido "
            f"(ammessi: {', '.join(sorted(VALID_EVENTS))})"
        )
    raw_actions = raw.get("do")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise RuleConfigError(f"Regola '{name}': 'do' deve essere una lista non vuota")
    actions = tuple(_parse_action(item, name) for item in raw_actions)

    devices = frozenset(a.device for a in actions)
    if known_devices is not None:
        unknown = devices - set(known_devices)
        if unknown:
            raise RuleConfigError(
                f"Regola '{name}': device non nel registry: {', '.join(sorted(unknown))}"
            )

    source = raw.get("source")
    return Rule(
        name=name,
        event=event,
        actions=actions,
        source=str(source).strip() if source else None,
        window=parse_time_window(raw.get("between")),
        cooldown_seconds=parse_duration(raw.get("cooldown")),
        devices=devices,
    )


def parse_rules(data, known_devices: set[str] | None = None) -> list[Rule]:
    """Valida una lista di regole già deserializzata. Solleva RuleConfigError."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise RuleConfigError("Il file regole deve contenere una lista di regole")
    rules = [_parse_rule(item, known_devices) for item in data]
    names = [r.name for r in rules]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise RuleConfigError(f"Nomi regola duplicati: {', '.join(sorted(duplicates))}")
    return rules


def load_rules(path: str | Path, known_devices: set[str] | None = None) -> list[Rule]:
    """Carica e valida le regole da un file YAML. File assente = nessuna regola."""
    rules_path = Path(path)
    if not rules_path.exists():
        return []
    try:
        import yaml  # noqa: PLC0415 — import pigro: dep opzionale a runtime
    except ImportError as exc:  # pragma: no cover - dipende dall'ambiente
        raise RuleConfigError(
            "PyYAML non installato: esegui 'poetry install' per usare le regole"
        ) from exc
    try:
        data = yaml.safe_load(rules_path.read_text())
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"YAML regole non valido: {exc}") from exc
    return parse_rules(data, known_devices)
