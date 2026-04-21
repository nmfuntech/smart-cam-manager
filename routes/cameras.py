from auth import rate_limit, require_auth, require_csrf
from flask import Blueprint, current_app, jsonify, render_template, request


cameras_bp = Blueprint("cameras", __name__)


def get_services():
    return current_app.config["services"]


def build_camera_payload() -> dict:
    services = get_services()
    wifi = services.features.wifi.get_current_wifi()
    return {
        "profiles": services.features.camera_profiles.list_profiles(),
        "active_profile_id": services.features.camera_profiles.get_active_profile_id(),
        "current_wifi": wifi,
    }


def apply_profile(profile_id: str) -> dict:
    services = get_services()
    profile = services.features.camera_profiles.activate_profile(profile_id)
    full_profile = services.features.camera_profiles.get_profile(profile_id)
    updates = services.features.camera_profiles.build_runtime_updates(full_profile)
    services.runtime_config.update(
        updates,
        allow_sensitive=True,
        allow_internal=True,
    )
    services.camera.apply_runtime_config(updates)
    services.ptz.apply_runtime_config(updates)
    services.motion.apply_runtime_config(updates)
    return profile


@cameras_bp.get("/api/cameras")
@require_auth(api=True)
def list_cameras():
    return jsonify(build_camera_payload())


@cameras_bp.get("/cameras")
@require_auth()
def cameras_page():
    return render_template("cameras.html")


@cameras_bp.get("/api/network/wifi")
@require_auth(api=True)
def current_wifi():
    return jsonify(get_services().features.wifi.get_current_wifi())


@cameras_bp.post("/api/cameras")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("camera-save", limit=12, window_seconds=60, api=True)
def save_camera():
    payload = request.get_json(silent=True) or {}
    try:
        profile = get_services().features.camera_profiles.save_profile(payload)
        if payload.get("activate"):
            profile = apply_profile(profile["id"])
        return jsonify(
            {
                "ok": True,
                "profile": profile,
                **build_camera_payload(),
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("Salvataggio camera fallito")
        return jsonify({"ok": False, "error": "Errore interno durante salvataggio camera"}), 500


@cameras_bp.post("/api/cameras/<profile_id>/activate")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("camera-activate", limit=20, window_seconds=60, api=True)
def activate_camera(profile_id: str):
    try:
        profile = apply_profile(profile_id)
        return jsonify({"ok": True, "profile": profile, **build_camera_payload()})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception:
        current_app.logger.exception("Attivazione camera fallita")
        return jsonify({"ok": False, "error": "Errore interno durante attivazione camera"}), 500
