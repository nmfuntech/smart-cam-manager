from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_file


motion_bp = Blueprint("motion", __name__)


def get_services():
    return current_app.config["services"]


@motion_bp.route("/motion_status")
def motion_status():
    return get_services().motion.get_status()


@motion_bp.get("/runtime_config")
def runtime_config():
    config = get_services().runtime_config.get_public_config()
    return jsonify({"config": config})


@motion_bp.patch("/api/runtime_config")
def update_runtime_config():
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates", {})
    if not isinstance(updates, dict) or not updates:
        return jsonify({"ok": False, "error": "Campo updates mancante o non valido"}), 400

    try:
        config = get_services().runtime_config.update(updates)
        get_services().camera.apply_runtime_config(updates)
        get_services().ptz.apply_runtime_config(updates)
        get_services().motion.apply_runtime_config(updates)
        return jsonify({"ok": True, "config": config})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Aggiornamento config fallito: {exc}"}), 500


@motion_bp.route("/latest_motion.jpg")
def latest_motion():
    events = get_services().motion.list_events(limit=1)
    if not events:
        return Response("Nessun evento di movimento salvato", status=404)
    return send_file(events[0]["preview_path"], mimetype="image/jpeg")


@motion_bp.route("/motion_captures")
def motion_captures():
    limit = request.args.get("limit", default=8, type=int)
    limit = max(1, min(limit, 200))
    all_events = get_services().motion.list_events(limit=500, include_frames=False)
    return jsonify(
        {
            "captures": all_events[:limit],
            "total": len(all_events),
            "limit": limit,
        }
    )


@motion_bp.route("/motion_event/<event_id>")
def motion_event(event_id: str):
    event = get_services().motion.get_event(event_id)
    if not event:
        abort(404)
    return jsonify(event)


@motion_bp.route("/motion_event/<event_id>/preview.jpg")
def motion_event_preview(event_id: str):
    event_dir = Path(get_services().motion.config["save_dir"]) / event_id
    preview_path = event_dir / "latest.jpg"
    if not preview_path.exists():
        preview_path = event_dir / "cover.jpg"
    if not preview_path.exists():
        abort(404)
    return send_file(preview_path, mimetype="image/jpeg")


@motion_bp.route("/motion_event/<event_id>/<filename>")
def motion_event_frame(event_id: str, filename: str):
    event_dir = Path(get_services().motion.config["save_dir"]) / event_id
    file_path = event_dir / filename
    if not file_path.exists():
        abort(404)
    return send_file(file_path, mimetype="image/jpeg")


@motion_bp.route("/motion_capture/<filename>")
def motion_capture(filename: str):
    capture_path = Path(get_services().motion.config["save_dir"]) / filename
    if not capture_path.exists():
        abort(404)
    return send_file(capture_path, mimetype="image/jpeg")
