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


# --- Zone di esclusione movimento -------------------------------------------


def _active_camera_id() -> str:
    services = _services()
    try:
        return services.features.camera_profiles.get_active_profile_id() or "default"
    except Exception:
        return "default"


@settings_bp.get("/api/motion_zones")
@require_auth(api=True)
def motion_zones_get():
    from blackframe.motion_zones import get_zones

    camera_id = _active_camera_id()
    return jsonify({"ok": True, "camera_id": camera_id, **get_zones(camera_id)})


@settings_bp.put("/api/motion_zones")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("motion-zones", limit=30, window_seconds=60, api=True)
def motion_zones_put():
    from blackframe import audit
    from blackframe.motion_zones import set_zones

    payload = request.get_json(silent=True) or {}
    camera_id = _active_camera_id()
    try:
        zones = set_zones(camera_id, payload.get("exclude", []))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    audit.record("web", "admin", "motion_zones", f"{len(zones['exclude'])} zone")
    return jsonify({"ok": True, "camera_id": camera_id, **zones})
