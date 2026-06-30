from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from blackframe.auth import rate_limit, require_auth, require_csrf
from blackframe.automation import DeviceError, DeviceRegistry, RuleConfigError, parse_rules
from blackframe.automation.events import CATEGORY_EVENT_MAP
from blackframe.automation.rules_store import (
    delete_rule_raw,
    load_rules_raw,
    save_rules_raw,
    set_rule_enabled,
    upsert_rule_raw,
)
from blackframe.automation.tuya_import import (
    build_registry_payloads,
    load_snapshot_by_id,
    load_tinytuya_devices,
    scan_lan_devices,
)

# Limite dimensione file caricati (devices.json/snapshot.json/bundle): sono testo
# JSON piccoli, oltre 2 MB è certamente un input ostile/errato.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024

automation_bp = Blueprint("automation", __name__)

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_RL = {"limit": 20, "window_seconds": 60, "api": True}

# Valid UI-selectable events (derived from CATEGORY_EVENT_MAP so it stays in sync)
_VALID_UI_EVENTS = frozenset(CATEGORY_EVENT_MAP.values())


def get_services():
    return current_app.config["services"]


def _validate_name(name: str, label: str = "Nome") -> None:
    if not name or not _NAME_RE.fullmatch(name):
        raise ValueError(f"{label}: usa solo lettere minuscole, cifre e underscore")


_REDACTED = "***"


def _redact_payload(payload: dict) -> dict:
    """Oscura i segreti in un payload device prima di restituirlo all'UI."""
    redacted = dict(payload)
    for field in ("local_key", "access_secret"):
        if redacted.get(field):
            redacted[field] = _REDACTED
    return redacted


def _read_upload(file_storage) -> bytes:
    """Legge un file caricato applicando un tetto di dimensione (anti-DoS)."""
    data = file_storage.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError("File troppo grande (max 2 MB)")
    return data


def _rename_device_in_rules(old_name: str, new_name: str) -> None:
    """Aggiorna a cascata i riferimenti al device rinominato nelle azioni regola."""
    rules = load_rules_raw()
    changed = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        for action in rule.get("do") or []:
            if isinstance(action, dict) and action.get("device") == old_name:
                action["device"] = new_name
                changed = True
    if changed:
        save_rules_raw(rules)


# ─── Status helper ───────────────────────────────────────────────────────────


def _build_status_payload(services) -> dict:
    engine = services.automation_engine
    registry = services.automation_registry
    enabled = os.getenv("AUTOMATION_ENABLED", "false").lower() == "true"
    return {
        "enabled": enabled,
        "active": engine is not None,
        "rule_count": len(engine.rules) if engine is not None else 0,
        "device_count": len(registry.device_names()) if registry is not None else 0,
    }


# ─── Page ────────────────────────────────────────────────────────────────────


@automation_bp.get("/automazione")
@require_auth()
def automazione_page():
    return render_template("automazione.html")


# ─── Status ──────────────────────────────────────────────────────────────────


@automation_bp.get("/api/automazione/status")
@require_auth(api=True)
def automazione_status():
    return jsonify({"ok": True, **_build_status_payload(get_services())})


# ─── Toggle ──────────────────────────────────────────────────────────────────


@automation_bp.patch("/api/automazione/toggle")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_toggle():
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload:
        return jsonify({"ok": False, "error": "Campo 'enabled' mancante"}), 400
    enabled = bool(payload["enabled"])
    services = get_services()
    try:
        services.runtime_config.update({"AUTOMATION_ENABLED": enabled})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    services.reload_automation()
    return jsonify({"ok": True, **_build_status_payload(services)})


# ─── Devices ─────────────────────────────────────────────────────────────────


@automation_bp.get("/api/automazione/devices")
@require_auth(api=True)
def automazione_list_devices():
    registry: DeviceRegistry | None = get_services().automation_registry
    if registry is None:
        return jsonify({"ok": True, "devices": []})
    return jsonify({"ok": True, "devices": registry.list_devices()})


@automation_bp.post("/api/automazione/devices")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_save_device():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    try:
        _validate_name(name, "Nome dispositivo")
        registry = get_services().automation_registry
        if registry is None:
            return jsonify({"ok": False, "error": "Registry non disponibile"}), 503
        device = registry.save_device(payload)
        get_services().reload_automation()
        return jsonify({"ok": True, "device": device})
    except (ValueError, DeviceError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@automation_bp.delete("/api/automazione/devices/<name>")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_delete_device(name: str):
    try:
        _validate_name(name, "Nome dispositivo")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    registry = get_services().automation_registry
    if registry is None:
        return jsonify({"ok": False, "error": "Registry non disponibile"}), 503
    if not registry.delete_device(name):
        return jsonify({"ok": False, "error": f"Dispositivo '{name}' non trovato"}), 404
    get_services().reload_automation()
    return jsonify({"ok": True})


@automation_bp.post("/api/automazione/devices/<name>/test")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_test_device(name: str):
    """Esegue un comando reale sul device (accendi/spegni) per verificarne il
    funzionamento. Bypassa engine e regole: tocca direttamente il driver."""
    try:
        _validate_name(name, "Nome dispositivo")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "turn_on").strip()
    if action not in {"turn_on", "turn_off"}:
        return jsonify({"ok": False, "error": "Azione test: turn_on o turn_off"}), 400
    registry = get_services().automation_registry
    if registry is None:
        return jsonify({"ok": False, "error": "Registry non disponibile"}), 503
    try:
        device = registry.get(name)
        getattr(device, action)()
    except DeviceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "action": action})


@automation_bp.post("/api/automazione/devices/<name>/rename")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_rename_device(name: str):
    """Rinomina un device e aggiorna a cascata i riferimenti nelle regole."""
    payload = request.get_json(silent=True) or {}
    new_name = str(payload.get("new_name") or "").strip()
    try:
        _validate_name(name, "Nome dispositivo")
        _validate_name(new_name, "Nuovo nome dispositivo")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    registry = get_services().automation_registry
    if registry is None:
        return jsonify({"ok": False, "error": "Registry non disponibile"}), 503
    try:
        device = registry.rename_device(name, new_name)
    except DeviceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _rename_device_in_rules(name, new_name)
    get_services().reload_automation()
    return jsonify({"ok": True, "device": device})


@automation_bp.post("/api/automazione/devices/scan")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_scan_devices():
    """Scansiona la LAN per device Tuya e restituisce un'anteprima (non salva)."""
    try:
        scan_time = float(os.getenv("TUYA_SCAN_TIMEOUT_SEC", "10"))
    except ValueError:
        scan_time = 10.0
    try:
        found = scan_lan_devices(scan_time)
    except ImportError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 501
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 504
    payloads, skipped = build_registry_payloads(found)
    return jsonify(
        {
            "ok": True,
            "found": len(found),
            "devices": [_redact_payload(p) for p in payloads],
            "skipped": skipped,
        }
    )


@automation_bp.post("/api/automazione/devices/import-tuya")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_import_tuya():
    """Importa device da file tinytuya caricati (devices.json + opz. snapshot.json).

    Anteprima per default; con campo form ``commit=1`` salva nel registry.
    """
    if "devices" not in request.files:
        return jsonify({"ok": False, "error": "File 'devices.json' mancante"}), 400
    try:
        devices_raw = _read_upload(request.files["devices"])
        snapshot_raw = None
        if "snapshot" in request.files and request.files["snapshot"].filename:
            snapshot_raw = _read_upload(request.files["snapshot"])
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        devices_file = tmp_dir / "devices.json"
        devices_file.write_bytes(devices_raw)
        snapshot_by_id: dict = {}
        try:
            scan_devices = load_tinytuya_devices(devices_file)
            if snapshot_raw is not None:
                snapshot_file = tmp_dir / "snapshot.json"
                snapshot_file.write_bytes(snapshot_raw)
                snapshot_by_id = load_snapshot_by_id(snapshot_file)
        except (ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": f"File tinytuya non valido: {exc}"}), 400

    payloads, skipped = build_registry_payloads(scan_devices, snapshot_by_id=snapshot_by_id)

    commit = str(request.form.get("commit") or "").strip() in {"1", "true", "yes"}
    if not commit:
        return jsonify(
            {
                "ok": True,
                "committed": False,
                "devices": [_redact_payload(p) for p in payloads],
                "skipped": skipped,
            }
        )

    registry = get_services().automation_registry
    if registry is None:
        return jsonify({"ok": False, "error": "Registry non disponibile"}), 503
    saved = []
    for payload in payloads:
        try:
            saved.append(registry.save_device(payload))
        except DeviceError as exc:
            skipped.append(f"{payload.get('name')}: {exc}")
    get_services().reload_automation()
    return jsonify({"ok": True, "committed": True, "devices": saved, "skipped": skipped})


# ─── Rules ───────────────────────────────────────────────────────────────────


@automation_bp.get("/api/automazione/rules")
@require_auth(api=True)
def automazione_list_rules():
    return jsonify({"ok": True, "rules": load_rules_raw()})


@automation_bp.post("/api/automazione/rules")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_save_rule():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    try:
        _validate_name(name, "Nome regola")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # Build clean rule dict from whitelisted fields only
    rule_dict: dict = {"name": name}

    on_event = str(payload.get("on") or "").strip()
    if not on_event:
        return jsonify({"ok": False, "error": "Campo 'on' (evento) obbligatorio"}), 400
    rule_dict["on"] = on_event

    cooldown = str(payload.get("cooldown") or "").strip()
    if cooldown:
        rule_dict["cooldown"] = cooldown

    between_from = str(payload.get("between_from") or "").strip()
    between_to = str(payload.get("between_to") or "").strip()
    if between_from and between_to:
        rule_dict["between"] = [between_from, between_to]

    source = str(payload.get("source") or "").strip()
    if source:
        rule_dict["source"] = source

    # enabled è opzionale: assente = True (regola attiva). Lo persistiamo solo se
    # esplicitamente disabilitata, per non sporcare lo YAML delle regole attive.
    if "enabled" in payload and not bool(payload["enabled"]):
        rule_dict["enabled"] = False

    raw_actions = payload.get("do")
    if not isinstance(raw_actions, list) or not raw_actions:
        return jsonify({"ok": False, "error": "Campo 'do' deve essere una lista non vuota"}), 400

    clean_actions = []
    for item in raw_actions:
        if not isinstance(item, dict):
            return jsonify({"ok": False, "error": "Ogni azione deve essere un oggetto"}), 400
        action: dict = {
            "device": str(item.get("device") or "").strip(),
            "action": str(item.get("action") or "").strip(),
        }
        for_val = str(item.get("for") or "").strip()
        if for_val:
            action["for"] = for_val
        # state è ammesso solo con action set_state (lampade): parse_rules valida
        # la coerenza. Accettiamo solo un dict non vuoto.
        state = item.get("state")
        if isinstance(state, dict) and state:
            action["state"] = state
        clean_actions.append(action)
    rule_dict["do"] = clean_actions

    # Validate against current device registry
    registry = get_services().automation_registry
    known = set(registry.device_names()) if registry is not None else None
    try:
        parse_rules([rule_dict], known_devices=known)
    except RuleConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    upsert_rule_raw(rule_dict)
    get_services().reload_automation()
    return jsonify({"ok": True, "rule": rule_dict})


@automation_bp.delete("/api/automazione/rules/<name>")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_delete_rule(name: str):
    try:
        _validate_name(name, "Nome regola")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not delete_rule_raw(name):
        return jsonify({"ok": False, "error": f"Regola '{name}' non trovata"}), 404
    get_services().reload_automation()
    return jsonify({"ok": True})


@automation_bp.post("/api/automazione/rules/<name>/test")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_test_rule(name: str):
    """Anteprima (``execute=false``) o esecuzione reale (``execute=true``) di una
    regola, ignorando match/finestra/cooldown. Richiede l'engine attivo."""
    try:
        _validate_name(name, "Nome regola")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    payload = request.get_json(silent=True) or {}
    execute = bool(payload.get("execute", False))
    engine = get_services().automation_engine
    if engine is None:
        return jsonify({"ok": False, "error": "Abilita l'automazione per testare le regole"}), 409
    planned = engine.run_rule(name, execute=execute)
    if planned is None:
        return jsonify({"ok": False, "error": f"Regola '{name}' non trovata"}), 404
    actions = [
        {"device": p.action.device, "action": p.action.action, "for": p.action.for_seconds}
        for p in planned
    ]
    return jsonify({"ok": True, "executed": execute, "actions": actions})


@automation_bp.patch("/api/automazione/rules/<name>/enabled")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_rule_enabled(name: str):
    try:
        _validate_name(name, "Nome regola")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload:
        return jsonify({"ok": False, "error": "Campo 'enabled' mancante"}), 400
    enabled = bool(payload["enabled"])
    if not set_rule_enabled(name, enabled):
        return jsonify({"ok": False, "error": f"Regola '{name}' non trovata"}), 404
    get_services().reload_automation()
    return jsonify({"ok": True, "enabled": enabled})


# ─── Import / Export ─────────────────────────────────────────────────────────


@automation_bp.get("/api/automazione/export")
@require_auth(api=True)
def automazione_export():
    """Scarica un bundle JSON con device (segreti redatti) e regole."""
    registry = get_services().automation_registry
    devices = registry.list_devices() if registry is not None else []
    bundle = {"version": 1, "devices": devices, "rules": load_rules_raw()}
    body = json.dumps(bundle, indent=2, ensure_ascii=False)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=blackframe-automazione.json"},
    )


@automation_bp.post("/api/automazione/import")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("automation", **_RL)
def automazione_import():
    """Importa un bundle JSON (device + regole). I segreti redatti ('***') sono
    trattati come vuoti: i device importati vanno completati con le chiavi."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Bundle JSON non valido"}), 400
    registry = get_services().automation_registry
    if registry is None:
        return jsonify({"ok": False, "error": "Registry non disponibile"}), 503

    errors: list[str] = []
    devices_in = payload.get("devices") or []
    rules_in = payload.get("rules") or []
    device_count = 0
    for entry in devices_in if isinstance(devices_in, list) else []:
        if not isinstance(entry, dict):
            continue
        clean = {k: ("" if v == _REDACTED else v) for k, v in entry.items()}
        name = str(clean.get("name") or "").strip()
        try:
            _validate_name(name, "Nome dispositivo")
            registry.save_device(clean)
            device_count += 1
        except (ValueError, DeviceError) as exc:
            errors.append(f"device {name or '?'}: {exc}")

    rule_count = 0
    if isinstance(rules_in, list) and rules_in:
        known = set(registry.device_names())
        for rule in rules_in:
            if not isinstance(rule, dict):
                continue
            name = str(rule.get("name") or "?").strip()
            try:
                parse_rules([rule], known_devices=known)
                upsert_rule_raw(rule)
                rule_count += 1
            except RuleConfigError as exc:
                errors.append(f"regola {name}: {exc}")

    get_services().reload_automation()
    return jsonify(
        {
            "ok": True,
            "devices_imported": device_count,
            "rules_imported": rule_count,
            "errors": errors,
        }
    )
