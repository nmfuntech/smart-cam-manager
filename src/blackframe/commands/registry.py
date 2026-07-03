"""Catalogo comandi condiviso, channel-agnostic.

Estrae la logica di business già presente in ``TelegramCommandBot._dispatch``
in funzioni pure (``services, arg -> CommandResult``), disaccoppiate da
Telegram (niente ``chat_id``, niente invio diretto di messaggi/media). Questo
è il "command registry" riusato sia dal bot Telegram sia dal layer agentico
(``blackframe.agent``): entrambi i canali guardano lo stesso elenco
``COMMAND_REGISTRY`` per sapere quali nomi comando esistono, che argomenti
accettano e come validarli — l'unica fonte di verità evita che i due canali
finiscano fuori sync.

I comandi puramente amministrativi di Telegram (``start``, ``help``, ``menu``,
``invite``, ``guests``, ``revoke``) restano fuori da questo registry: non sono
"azioni su device/sistema" e non devono mai poter essere suggeriti da un LLM.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from blackframe.automation import DeviceError
from blackframe.automation.rules_store import load_rules_raw, set_rule_enabled
from blackframe.envutil import env_bool as _env_bool
from blackframe.envutil import env_int

from .naming import normalize_identifier, resolve_name

logger = logging.getLogger(__name__)

# Stessa regex usata da routes/automation.py e telegram_commands.py per nomi
# device/regola: lettere minuscole, cifre, underscore.
_NAME_RE = re.compile(r"^[a-z0-9_]+$")

SENSITIVITY_PRESETS = {
    "alta": 15,
    "media": 30,
    "bassa": 50,
    "high": 15,
    "medium": 30,
    "low": 50,
}


# Pavimento di default specifico del modulo; il parsing vive in envutil.
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    return env_int(name, default, minimum=minimum)


@dataclass
class CommandArgSpec:
    kind: Literal["none", "enum", "name", "int", "float"]
    enum: tuple[str, ...] = ()
    required: bool = True
    # Per kind=="name": "device" o "rule" — indica quale elenco di nomi noti
    # usare per il grounding del prompt LLM e la risoluzione tollerante.
    name_source: str | None = None


@dataclass
class CommandResult:
    text: str | None = None
    photo: bytes | None = None
    video: bytes | None = None
    caption: str | None = None


@dataclass
class CommandSpec:
    name: str
    description: str
    arg: CommandArgSpec | None
    readonly: bool
    handler: Callable[[Any, str | None], CommandResult] | None = field(default=None)


def _known_names_for(spec: CommandArgSpec, services: Any) -> list[str] | None:
    if spec.name_source == "device":
        registry = getattr(services, "automation_registry", None)
        if registry is None:
            return None
        return registry.device_names()
    if spec.name_source == "rule":
        return [r.get("name") for r in load_rules_raw() if isinstance(r, dict) and r.get("name")]
    return None


def validate_arg(spec: CommandArgSpec | None, raw: str | None, services: Any = None) -> str | None:
    """Normalizza/valida un argomento grezzo contro lo schema del comando.

    Solleva ``ValueError`` con un messaggio in italiano su qualunque input non
    conforme: sia il parsing testuale (Telegram) sia l'output dell'LLM (agente)
    passano da qui prima di poter raggiungere ``execute``.

    Per ``kind=="name"``, ``raw`` viene prima normalizzato (spazi/accenti/
    preposizioni) e, se ``services`` è fornito, risolto contro l'elenco reale
    dei nomi noti (device o regole): un input come "la lampada dell'ingresso"
    può così risolvere a ``lampada_ingresso`` senza che l'utente/il modello
    debbano indovinare lo slug esatto.
    """
    if spec is None or spec.kind == "none":
        return None
    raw = (raw or "").strip()
    if not raw:
        if spec.required:
            raise ValueError("Argomento obbligatorio mancante")
        return None
    if spec.kind == "enum":
        value = raw.lower()
        if value not in spec.enum:
            raise ValueError(f"Valore non valido, usa uno tra: {', '.join(spec.enum)}")
        return value
    if spec.kind == "name":
        normalized = normalize_identifier(raw)
        if not normalized or not _NAME_RE.fullmatch(normalized):
            raise ValueError(
                "Nome non valido: usa solo lettere, cifre e underscore (es. lampada_ingresso)"
            )
        if services is not None and spec.name_source:
            candidates = _known_names_for(spec, services)
            if candidates:
                resolved, suggestions = resolve_name(normalized, candidates)
                if resolved is not None:
                    return resolved
                if suggestions:
                    raise ValueError(
                        f"Nome non trovato. Forse intendevi: {', '.join(suggestions)}?"
                    )
                raise ValueError(
                    f"Nome non trovato tra quelli disponibili: {', '.join(candidates)}"
                )
        return normalized
    if spec.kind == "int":
        try:
            int(float(raw))
        except ValueError as exc:
            raise ValueError("Serve un numero intero") from exc
        return raw
    if spec.kind == "float":
        try:
            float(raw)
        except ValueError as exc:
            raise ValueError("Serve un numero") from exc
        return raw
    raise ValueError(f"Tipo argomento sconosciuto: {spec.kind}")


# --- helper condivisi (mirror di TelegramCommandBot) -------------------------


def _notifier(services: Any) -> Any | None:
    features = getattr(services, "features", None)
    return getattr(features, "telegram", None) if features is not None else None


def _notifications_state(services: Any) -> str:
    if not _env_bool("NOTIFY_TELEGRAM_ENABLED", False):
        return "spente"
    notifier = _notifier(services)
    remaining = notifier.muted_remaining() if notifier is not None else 0
    if remaining > 0:
        return f"in pausa ({int(remaining // 60) + 1} min)"
    return "attive"


def _apply_runtime_updates(services: Any, updates: dict[str, object]) -> None:
    services.runtime_config.update(updates)
    services.apply_runtime_config_all(updates)
    if "CONTINUOUS_RECORD_ENABLED" in updates and getattr(services, "continuous", None):
        services.continuous.apply_config(services.motion.config)


# --- handler: stato / informativi --------------------------------------------


def _status(services: Any, arg: str | None) -> CommandResult:
    motion_svc = services.motion
    config = getattr(motion_svc, "config", {}) or {}
    classifier = getattr(motion_svc, "classifier", None)

    stream = services.camera.get_status()
    motion = motion_svc.get_status()
    ptz = services.ptz.get_status()
    continuous = (
        services.continuous.status()
        if getattr(services, "continuous", None) is not None
        else {"active": False}
    )

    stream_state = stream.get("connection_state") or (
        "online" if stream.get("connected") else "offline"
    )
    motion_on = bool(motion.get("enabled"))
    moving = "sì" if motion.get("motion_detected") else "no"
    ptz_state = "ok" if ptz.get("available") else (ptz.get("error") or "non disponibile")

    lines = [
        "🎥 BLACKFRAME — Stato",
        "",
        f"📹 Stream: {stream_state}",
        f"👁 Movimento: {'attivo' if motion_on else 'spento'} (in corso: {moving})",
    ]

    if config.get("classification_enabled"):
        backend = config.get("classification_backend", "?")
        line = f"🧠 Classificazione: attiva · {backend}"
        if classifier is not None and not classifier.ready:
            line += " ⚠️ modello assente"
        lines.append(line)
        if classifier is not None:
            targets = getattr(classifier, "targets", set())
            persona = "sì" if classifier.LABEL_PERSONA in targets else "no"
            animali = "sì" if classifier.LABEL_PET in targets else "no"
            lines.append(f"      🧍 Persone: {persona}   🐕 Animali: {animali}")
    else:
        lines.append("🧠 Classificazione: spenta")

    rec = "sì" if config.get("record_enabled") else "no"
    cont = "sì" if continuous.get("active") else "no"
    lines.extend(
        [
            f"🔔 Notifiche: {_notifications_state(services)}",
            f"⏺ Registrazione: clip evento {rec} · continua {cont}",
            f"🕹 PTZ: {ptz_state}",
            f"🕓 Ultimo evento: {motion.get('last_motion_at') or '-'}",
        ]
    )
    return CommandResult(text="\n".join(lines))


def _config(services: Any, arg: str | None) -> CommandResult:
    def flag(name: str, on: str, off: str, default: bool = False) -> str:
        return on if _env_bool(name, default) else off

    text = "\n".join(
        [
            "Impostazioni BLACKFRAME",
            f"Movimento: {flag('MOTION_ENABLED', 'attivo', 'spento', True)}",
            f"Soglia movimento: {_env_int('MOTION_THRESHOLD', 30, 0)}",
            f"Notifiche: {_notifications_state(services)}",
            f"Riconoscimento: {flag('CLASSIFICATION_ENABLED', 'attivo', 'spento')}",
            f"Persone: {flag('CLASSIFICATION_DETECT_PERSON', 'si', 'no', True)}"
            f" | Animali: {flag('CLASSIFICATION_DETECT_PET', 'si', 'no', True)}",
            f"Clip evento: {flag('RECORD_ENABLED', 'attive', 'spente')}",
            f"Reg. continua: {flag('CONTINUOUS_RECORD_ENABLED', 'attiva', 'spenta')}",
        ]
    )
    return CommandResult(text=text)


def _snapshot(services: Any, arg: str | None) -> CommandResult:
    frame = services.camera.get_frame()
    if frame is None:
        return CommandResult(text="Nessun frame disponibile.")
    return CommandResult(photo=frame, caption="Snapshot live BLACKFRAME")


def _latest(services: Any, arg: str | None) -> CommandResult:
    events = services.motion.list_events(limit=1)
    if not events:
        return CommandResult(text="Nessun evento salvato.")
    event = events[0]
    preview = Path(str(event.get("preview_path") or ""))
    if not preview.is_file():
        return CommandResult(text="Anteprima ultimo evento non disponibile.")
    try:
        photo = preview.read_bytes()
    except OSError:
        return CommandResult(text="Anteprima ultimo evento non leggibile.")
    caption = f"Ultimo evento: {event.get('label') or event.get('id')}"
    return CommandResult(photo=photo, caption=caption)


def _events(services: Any, arg: str | None) -> CommandResult:
    events = services.motion.list_events(limit=5)
    if not events:
        return CommandResult(text="Nessun evento salvato.")
    lines = ["Ultimi eventi:"]
    for event in events:
        label = event.get("label") or event.get("id")
        frames = event.get("frame_count")
        classification = (event.get("classification") or {}).get("class_label")
        suffix = f" ({classification})" if classification else ""
        frame_text = f", {frames} frame" if frames is not None else ""
        lines.append(f"- {label}{suffix}{frame_text}")
    return CommandResult(text="\n".join(lines))


# --- handler: rilevamento / notifiche / registrazione -------------------------


def _make_bool_toggle(
    key: str, value: bool, success: str
) -> Callable[[Any, str | None], CommandResult]:
    def handler(services: Any, arg: str | None) -> CommandResult:
        try:
            _apply_runtime_updates(services, {key: value})
        except ValueError as exc:
            return CommandResult(text=f"Config non valida: {exc}")
        except Exception:
            logger.exception("Aggiornamento runtime fallito (%s=%s)", key, value)
            return CommandResult(text="Aggiornamento fallito.")
        return CommandResult(text=success)

    return handler


def _sensitivity(services: Any, arg: str | None) -> CommandResult:
    preset = (arg or "").lower()
    threshold = SENSITIVITY_PRESETS.get(preset)
    if threshold is None:
        return CommandResult(text="Preset sconosciuto. Usa: bassa, media o alta.")
    try:
        _apply_runtime_updates(services, {"MOTION_THRESHOLD": threshold})
    except ValueError as exc:
        return CommandResult(text=f"Config non valida: {exc}")
    except Exception:
        logger.exception("Aggiornamento sensibilita fallito")
        return CommandResult(text="Aggiornamento fallito.")
    return CommandResult(text=f"Sensibilita impostata su {preset} (soglia {threshold}).")


def _mute(services: Any, arg: str | None) -> CommandResult:
    notifier = _notifier(services)
    if notifier is None:
        return CommandResult(text="Notifiche non disponibili.")
    minutes = 15.0
    if arg:
        try:
            minutes = float(arg)
        except ValueError:
            return CommandResult(text="Uso: indica i minuti di pausa (numero).")
    if minutes <= 0:
        return CommandResult(text="Indica un numero di minuti positivo.")
    notifier.mute(minutes * 60)
    return CommandResult(text=f"Notifiche silenziate per {int(minutes)} min.")


def _resume(services: Any, arg: str | None) -> CommandResult:
    notifier = _notifier(services)
    if notifier is None:
        return CommandResult(text="Notifiche non disponibili.")
    notifier.mute(0)
    return CommandResult(text="Notifiche riprese.")


# --- handler: PTZ --------------------------------------------------------------


def _make_ptz_move(direction: str) -> Callable[[Any, str | None], CommandResult]:
    def handler(services: Any, arg: str | None) -> CommandResult:
        success, error = services.ptz.move(direction)
        return CommandResult(text="PTZ mosso." if success else f"PTZ fallito: {error}")

    return handler


def _ptz_stop(services: Any, arg: str | None) -> CommandResult:
    success, error = services.ptz.stop()
    return CommandResult(text="PTZ fermato." if success else f"Stop PTZ fallito: {error}")


def _ptz_home(services: Any, arg: str | None) -> CommandResult:
    success, error = services.ptz.home()
    return CommandResult(text="PTZ riportato home." if success else f"PTZ home fallito: {error}")


# --- handler: domotica (device + regole) ---------------------------------------


def _devices(services: Any, arg: str | None) -> CommandResult:
    registry = getattr(services, "automation_registry", None)
    if registry is None:
        return CommandResult(text="Automazione non disponibile.")
    names = registry.device_names()
    if not names:
        return CommandResult(text="Nessun dispositivo configurato.")
    return CommandResult(text="🏠 Dispositivi:\n" + "\n".join(f"- {name}" for name in names))


def _make_device_action(action: str) -> Callable[[Any, str | None], CommandResult]:
    verb = "acceso" if action == "turn_on" else "spento"
    usage = f"Uso: /device_{'on' if action == 'turn_on' else 'off'} <nome>"

    def handler(services: Any, arg: str | None) -> CommandResult:
        registry = getattr(services, "automation_registry", None)
        if registry is None:
            return CommandResult(text="Automazione non disponibile.")
        normalized = normalize_identifier((arg or "").strip())
        if not normalized or not _NAME_RE.fullmatch(normalized):
            return CommandResult(text=usage)
        name, suggestions = resolve_name(normalized, registry.device_names())
        if name is None:
            if suggestions:
                return CommandResult(
                    text=f"Dispositivo '{normalized}' non trovato. "
                    f"Forse intendevi: {', '.join(suggestions)}?"
                )
            return CommandResult(text=f"Dispositivo '{normalized}' non trovato. Usa /devices.")
        try:
            getattr(registry.get(name), action)()
        except DeviceError as exc:
            return CommandResult(text=f"Errore dispositivo '{name}': {exc}")
        return CommandResult(text=f"Dispositivo '{name}' {verb}.")

    return handler


def _rules(services: Any, arg: str | None) -> CommandResult:
    rules = load_rules_raw()
    if not rules:
        return CommandResult(text="Nessuna regola configurata.")
    lines = ["📜 Regole:"]
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        name = rule.get("name") or "?"
        event = rule.get("on") or "?"
        state = "on" if rule.get("enabled", True) else "off"
        lines.append(f"- {name} · {event} · {state}")
    return CommandResult(text="\n".join(lines))


def _rule_names() -> list[str]:
    return [r.get("name") for r in load_rules_raw() if isinstance(r, dict) and r.get("name")]


def _resolve_rule_name(arg: str | None) -> tuple[str | None, str | None]:
    """Risolve l'arg grezzo a un nome regola esistente, o un messaggio d'errore."""
    normalized = normalize_identifier((arg or "").strip())
    if not normalized or not _NAME_RE.fullmatch(normalized):
        return None, None
    name, suggestions = resolve_name(normalized, _rule_names())
    if name is not None:
        return name, None
    if suggestions:
        return (
            None,
            f"Regola '{normalized}' non trovata. Forse intendevi: {', '.join(suggestions)}?",
        )
    return None, f"Regola '{normalized}' non trovata. Usa /rules."


def _rule_run(services: Any, arg: str | None) -> CommandResult:
    normalized = normalize_identifier((arg or "").strip())
    if not normalized or not _NAME_RE.fullmatch(normalized):
        return CommandResult(text="Uso: /rule_run <nome>")
    engine = getattr(services, "automation_engine", None)
    if engine is None:
        return CommandResult(text="Automazione disabilitata: abilitala per eseguire le regole.")
    name, error = _resolve_rule_name(arg)
    if name is None:
        return CommandResult(text=error or "Uso: /rule_run <nome>")
    planned = engine.run_rule(name, execute=True)
    if planned is None:
        return CommandResult(text=f"Regola '{name}' non trovata.")
    return CommandResult(text=f"Regola '{name}' eseguita ({len(planned)} azioni).")


def _make_rule_enabled(enabled: bool) -> Callable[[Any, str | None], CommandResult]:
    usage = f"Uso: /rule_{'on' if enabled else 'off'} <nome>"

    def handler(services: Any, arg: str | None) -> CommandResult:
        name, error = _resolve_rule_name(arg)
        if name is None:
            return CommandResult(text=error or usage)
        if not set_rule_enabled(name, enabled):
            return CommandResult(text=f"Regola '{name}' non trovata.")
        reload_automation = getattr(services, "reload_automation", None)
        if callable(reload_automation):
            reload_automation()
        return CommandResult(text=f"Regola '{name}' {'abilitata' if enabled else 'disabilitata'}.")

    return handler


# --- registro -------------------------------------------------------------------

_NONE_ARG = CommandArgSpec(kind="none")
_DEVICE_NAME_ARG = CommandArgSpec(kind="name", required=True, name_source="device")
_RULE_NAME_ARG = CommandArgSpec(kind="name", required=True, name_source="rule")

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "status": CommandSpec("status", "Stato camera e movimento", None, True, _status),
    "config": CommandSpec("config", "Riepilogo impostazioni", None, True, _config),
    "snapshot": CommandSpec("snapshot", "Invia foto live", None, True, _snapshot),
    "latest": CommandSpec("latest", "Invia ultimo evento", None, True, _latest),
    "events": CommandSpec("events", "Elenca ultimi eventi", None, True, _events),
    "motion_on": CommandSpec(
        "motion_on",
        "Attiva rilevamento movimento",
        None,
        False,
        _make_bool_toggle("MOTION_ENABLED", True, "Rilevamento movimento attivato."),
    ),
    "motion_off": CommandSpec(
        "motion_off",
        "Disattiva rilevamento movimento",
        None,
        False,
        _make_bool_toggle("MOTION_ENABLED", False, "Rilevamento movimento disattivato."),
    ),
    "sensitivity": CommandSpec(
        "sensitivity",
        "Sensibilita movimento: bassa, media o alta",
        CommandArgSpec(kind="enum", enum=tuple(SENSITIVITY_PRESETS)),
        False,
        _sensitivity,
    ),
    "classification_on": CommandSpec(
        "classification_on",
        "Attiva riconoscimento persone/animali",
        None,
        False,
        _make_bool_toggle("CLASSIFICATION_ENABLED", True, "Riconoscimento attivato."),
    ),
    "classification_off": CommandSpec(
        "classification_off",
        "Disattiva riconoscimento persone/animali",
        None,
        False,
        _make_bool_toggle("CLASSIFICATION_ENABLED", False, "Riconoscimento disattivato."),
    ),
    "detect_person_on": CommandSpec(
        "detect_person_on",
        "Notifica quando rileva una persona",
        None,
        False,
        _make_bool_toggle("CLASSIFICATION_DETECT_PERSON", True, "Notifica persone attivata."),
    ),
    "detect_person_off": CommandSpec(
        "detect_person_off",
        "Ignora le persone rilevate",
        None,
        False,
        _make_bool_toggle("CLASSIFICATION_DETECT_PERSON", False, "Notifica persone disattivata."),
    ),
    "detect_pet_on": CommandSpec(
        "detect_pet_on",
        "Notifica quando rileva un animale domestico",
        None,
        False,
        _make_bool_toggle("CLASSIFICATION_DETECT_PET", True, "Notifica animali attivata."),
    ),
    "detect_pet_off": CommandSpec(
        "detect_pet_off",
        "Ignora gli animali rilevati",
        None,
        False,
        _make_bool_toggle("CLASSIFICATION_DETECT_PET", False, "Notifica animali disattivata."),
    ),
    "notifications_on": CommandSpec(
        "notifications_on",
        "Attiva le notifiche Telegram",
        None,
        False,
        _make_bool_toggle("NOTIFY_TELEGRAM_ENABLED", True, "Notifiche attivate."),
    ),
    "notifications_off": CommandSpec(
        "notifications_off",
        "Disattiva le notifiche Telegram",
        None,
        False,
        _make_bool_toggle("NOTIFY_TELEGRAM_ENABLED", False, "Notifiche disattivate."),
    ),
    "mute": CommandSpec(
        "mute",
        "Silenzia le notifiche per N minuti (default 15)",
        CommandArgSpec(kind="float", required=False),
        False,
        _mute,
    ),
    "resume": CommandSpec("resume", "Riprendi le notifiche silenziate", None, False, _resume),
    "record_on": CommandSpec(
        "record_on",
        "Attiva la clip video per ogni evento",
        None,
        False,
        _make_bool_toggle("RECORD_ENABLED", True, "Clip video evento attivate."),
    ),
    "record_off": CommandSpec(
        "record_off",
        "Disattiva la clip video per ogni evento",
        None,
        False,
        _make_bool_toggle("RECORD_ENABLED", False, "Clip video evento disattivate."),
    ),
    "continuous_on": CommandSpec(
        "continuous_on",
        "Attiva la registrazione continua",
        None,
        False,
        _make_bool_toggle("CONTINUOUS_RECORD_ENABLED", True, "Registrazione continua attivata."),
    ),
    "continuous_off": CommandSpec(
        "continuous_off",
        "Disattiva la registrazione continua",
        None,
        False,
        _make_bool_toggle(
            "CONTINUOUS_RECORD_ENABLED", False, "Registrazione continua disattivata."
        ),
    ),
    "ptz_left": CommandSpec(
        "ptz_left", "Muovi la camera a sinistra", None, False, _make_ptz_move("left")
    ),
    "ptz_right": CommandSpec(
        "ptz_right", "Muovi la camera a destra", None, False, _make_ptz_move("right")
    ),
    "ptz_up": CommandSpec("ptz_up", "Muovi la camera in alto", None, False, _make_ptz_move("up")),
    "ptz_down": CommandSpec(
        "ptz_down", "Muovi la camera in basso", None, False, _make_ptz_move("down")
    ),
    "ptz_stop": CommandSpec("ptz_stop", "Ferma il movimento PTZ", None, False, _ptz_stop),
    "ptz_home": CommandSpec(
        "ptz_home", "Riporta la camera in posizione home", None, False, _ptz_home
    ),
    "devices": CommandSpec("devices", "Elenca i dispositivi smart home", None, True, _devices),
    "device_on": CommandSpec(
        "device_on",
        "Accendi un dispositivo per nome",
        _DEVICE_NAME_ARG,
        False,
        _make_device_action("turn_on"),
    ),
    "device_off": CommandSpec(
        "device_off",
        "Spegni un dispositivo per nome",
        _DEVICE_NAME_ARG,
        False,
        _make_device_action("turn_off"),
    ),
    "rules": CommandSpec("rules", "Elenca le regole di automazione", None, True, _rules),
    "rule_run": CommandSpec(
        "rule_run", "Esegui subito una regola per nome", _RULE_NAME_ARG, False, _rule_run
    ),
    "rule_on": CommandSpec(
        "rule_on", "Abilita una regola per nome", _RULE_NAME_ARG, False, _make_rule_enabled(True)
    ),
    "rule_off": CommandSpec(
        "rule_off",
        "Disabilita una regola per nome",
        _RULE_NAME_ARG,
        False,
        _make_rule_enabled(False),
    ),
    # Catalogo-only: la clip on-demand resta gestita da telegram_commands.py
    # (registrazione asincrona + invio video threadato). Elencata qui solo
    # perche' l'agente sappia che esiste e non provi a "inventarla" come
    # comando sconosciuto quando l'utente chiede una clip.
    "clip": CommandSpec(
        "clip",
        "Registra e invia una clip live di N secondi (default 10, max 30)",
        CommandArgSpec(kind="int", required=False),
        True,
        None,
    ),
}


def execute(name: str, arg: str | None, services: Any) -> CommandResult:
    """Esegue un comando del registro. Il chiamante deve aver già validato
    ``name``/``arg`` (whitelist + ``validate_arg``): qui non si torna più
    indietro sulla legittimità dell'input, solo sull'esecuzione."""
    spec = COMMAND_REGISTRY.get(name)
    if spec is None or spec.handler is None:
        raise ValueError(f"Comando '{name}' non eseguibile dal registro")
    return spec.handler(services, arg)
