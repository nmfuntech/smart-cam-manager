"""Unified settings page and performance-profile APIs."""

from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, render_template, request

from blackframe.auth import rate_limit, require_auth, require_csrf
from blackframe.performance_profiles import PerformanceProfileManager

settings_bp = Blueprint("settings", __name__)


@settings_bp.get("/impostazioni")
@require_auth()
def impostazioni_page():
    return render_template("impostazioni.html")


def _services():
    return current_app.config["services"]


def _manager() -> PerformanceProfileManager:
    return PerformanceProfileManager(
        _services().runtime_config,
        catalog_path=os.getenv(
            "PERFORMANCE_PROFILE_CATALOG_PATH", "config/performance_profiles.yaml"
        ),
        state_path=os.getenv("PERFORMANCE_PROFILE_STATE_PATH", "data/performance_profile.json"),
    )


def _camera_count() -> int:
    services = _services()
    return max(1, 1 + len(getattr(services, "monitors", {}) or {}))


@settings_bp.get("/api/performance_profiles")
@require_auth(api=True)
def performance_profiles():
    try:
        return jsonify({"ok": True, **_manager().list_profiles(_camera_count())})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@settings_bp.post("/api/performance_profiles/preview")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("performance-profile-preview", limit=30, window_seconds=60, api=True)
def performance_profile_preview():
    payload = request.get_json(silent=True) or {}
    profile = str(payload.get("profile") or "").strip()
    try:
        return jsonify({"ok": True, **_manager().preview(profile)})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@settings_bp.post("/api/performance_profiles/apply")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("performance-profile-apply", limit=5, window_seconds=60, api=True)
def performance_profile_apply():
    payload = request.get_json(silent=True) or {}
    profile = str(payload.get("profile") or "").strip()
    try:
        result = _manager().apply(profile)
        updates = result.pop("updates")
        live_apply_ok = True
        try:
            _services().apply_runtime_config_all(updates)
        except Exception:
            live_apply_ok = False
            current_app.logger.exception(
                "Profilo salvato ma applicazione live incompleta; serve riavvio"
            )
            result["restart_required"] = sorted(
                set(result["restart_required"]) | set(updates)
            )
        agent = getattr(_services(), "agent", None)
        if agent is not None:
            agent.start_warmup()
        return jsonify(
            {
                "ok": True,
                **result,
                "live_apply_ok": live_apply_ok,
                "config": _services().runtime_config.get_public_config(),
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("Applicazione profilo prestazioni fallita")
        return jsonify({"ok": False, "error": "Applicazione profilo fallita"}), 500
