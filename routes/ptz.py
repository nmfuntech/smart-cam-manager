from flask import Blueprint, current_app, jsonify


ptz_bp = Blueprint("ptz", __name__)


def get_services():
    return current_app.config["services"]


@ptz_bp.route("/ptz_status")
def ptz_status():
    return get_services().ptz.get_status()


@ptz_bp.post("/api/ptz/<direction>")
def move_ptz(direction: str):
    success, error = get_services().ptz.move(direction)
    status_code = 200 if success else 400
    return jsonify({"ok": success, "error": error}), status_code


@ptz_bp.post("/api/ptz/stop")
def stop_ptz():
    success, error = get_services().ptz.stop()
    status_code = 200 if success else 400
    return jsonify({"ok": success, "error": error}), status_code


@ptz_bp.post("/api/ptz/home")
def home_ptz():
    success, error = get_services().ptz.home()
    status_code = 200 if success else 400
    return jsonify({"ok": success, "error": error}), status_code
