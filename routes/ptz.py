from auth import rate_limit, require_auth, require_csrf
from flask import Blueprint, current_app, jsonify


ptz_bp = Blueprint("ptz", __name__)


def get_services():
    return current_app.config["services"]


@ptz_bp.route("/ptz_status")
@require_auth(api=True)
def ptz_status():
    return get_services().ptz.get_status()


@ptz_bp.post("/api/ptz/<direction>")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("ptz-command", limit=40, window_seconds=10, api=True)
def move_ptz(direction: str):
    success, error = get_services().ptz.move(direction)
    status_code = 200 if success else 400
    return jsonify({"ok": success, "error": error}), status_code


@ptz_bp.post("/api/ptz/stop")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("ptz-command", limit=40, window_seconds=10, api=True)
def stop_ptz():
    success, error = get_services().ptz.stop()
    status_code = 200 if success else 400
    return jsonify({"ok": success, "error": error}), status_code


@ptz_bp.post("/api/ptz/home")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("ptz-home", limit=10, window_seconds=60, api=True)
def home_ptz():
    success, error = get_services().ptz.home()
    status_code = 200 if success else 400
    return jsonify({"ok": success, "error": error}), status_code
