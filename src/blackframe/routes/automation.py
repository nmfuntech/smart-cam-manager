from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

from blackframe.auth import rate_limit, require_auth, require_csrf
from blackframe.automation import DeviceError, DeviceRegistry, RuleConfigError, parse_rules
from blackframe.automation.events import CATEGORY_EVENT_MAP

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


# ─── YAML helpers ────────────────────────────────────────────────────────────


def _rules_path() -> str:
    return os.getenv("AUTOMATION_RULES_PATH", "config/automation/rules.yaml")


def _load_rules_raw() -> list[dict]:
    path = Path(_rules_path())
    if not path.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            return []
        # PyYAML 1.1 parses bare `on:` key as Python True — normalize back to "on"
        normalized = []
        for rule in data:
            if isinstance(rule, dict) and True in rule and "on" not in rule:
                rule = {("on" if k is True else k): v for k, v in rule.items()}
            normalized.append(rule)
        return normalized
    except Exception:
        current_app.logger.exception("Lettura rules.yaml fallita")
        return []


def _save_rules_raw(rules: list[dict]) -> None:
    import yaml

    path = Path(_rules_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(
        rules or [],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    path.write_text(content, encoding="utf-8")


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


# ─── Rules ───────────────────────────────────────────────────────────────────


@automation_bp.get("/api/automazione/rules")
@require_auth(api=True)
def automazione_list_rules():
    return jsonify({"ok": True, "rules": _load_rules_raw()})


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
        clean_actions.append(action)
    rule_dict["do"] = clean_actions

    # Validate against current device registry
    registry = get_services().automation_registry
    known = set(registry.device_names()) if registry is not None else None
    try:
        parse_rules([rule_dict], known_devices=known)
    except RuleConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # Upsert: replace existing rule with same name, then append
    existing = [r for r in _load_rules_raw() if r.get("name") != name]
    existing.append(rule_dict)
    _save_rules_raw(existing)
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

    existing = _load_rules_raw()
    updated = [r for r in existing if r.get("name") != name]
    if len(updated) == len(existing):
        return jsonify({"ok": False, "error": f"Regola '{name}' non trovata"}), 404
    _save_rules_raw(updated)
    get_services().reload_automation()
    return jsonify({"ok": True})
