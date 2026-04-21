from pathlib import Path
import re

from auth import rate_limit, require_auth, require_csrf
from flask import Blueprint, Response, abort, current_app, jsonify, request, send_file


motion_bp = Blueprint("motion", __name__)
EVENT_ID_PATTERN = re.compile(r"^motion_event_\d{8}_\d{6}$")
EVENT_FRAME_PATTERN = re.compile(r"^(cover|latest|frame_\d{8}_\d{6}_\d{3})\.jpg$")
LEGACY_CAPTURE_PATTERN = re.compile(r"^motion_\d{8}_\d{6}\.jpg$")
PUBLIC_RUNTIME_UPDATE_KEYS = {
    "MOTION_ENABLED",
    "MOTION_THRESHOLD",
    "MOTION_MIN_AREA",
}


def get_services():
    return current_app.config["services"]


def _motion_root() -> Path:
    return Path(get_services().motion.config["save_dir"]).resolve()


def _resolve_motion_file(path: Path) -> Path:
    root = _motion_root()
    candidate = path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        abort(404)
    if not candidate.is_file():
        abort(404)
    return candidate


@motion_bp.route("/motion_status")
@require_auth(api=True)
def motion_status():
    return get_services().motion.get_status()


@motion_bp.get("/runtime_config")
@require_auth(api=True)
def runtime_config():
    config = get_services().runtime_config.get_public_config()
    return jsonify({"config": config})


@motion_bp.patch("/api/runtime_config")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("runtime-config", limit=20, window_seconds=60, api=True)
def update_runtime_config():
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates", {})
    if not isinstance(updates, dict) or not updates:
        return jsonify({"ok": False, "error": "Campo updates mancante o non valido"}), 400
    invalid_keys = sorted(set(updates) - PUBLIC_RUNTIME_UPDATE_KEYS)
    if invalid_keys:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "Parametro non consentito: "
                        + ", ".join(invalid_keys)
                    ),
                }
            ),
            400,
        )

    try:
        config = get_services().runtime_config.update(updates)
        get_services().camera.apply_runtime_config(updates)
        get_services().ptz.apply_runtime_config(updates)
        get_services().motion.apply_runtime_config(updates)
        return jsonify({"ok": True, "config": config})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("Aggiornamento config fallito")
        return jsonify({"ok": False, "error": "Errore interno durante aggiornamento config"}), 500


@motion_bp.route("/latest_motion.jpg")
@require_auth()
def latest_motion():
    events = get_services().motion.list_events(limit=1)
    if not events:
        return Response("Nessun evento di movimento salvato", status=404)
    preview_path = _resolve_motion_file(Path(events[0]["preview_path"]))
    return send_file(preview_path, mimetype="image/jpeg")


@motion_bp.route("/motion_captures")
@require_auth(api=True)
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


@motion_bp.delete("/api/motion_captures")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("motion-clear", limit=10, window_seconds=60, api=True)
def delete_motion_captures():
    removed = get_services().motion.clear_events()
    return jsonify({"ok": True, "removed": removed})


@motion_bp.route("/motion_event/<event_id>")
@require_auth(api=True)
def motion_event(event_id: str):
    event = get_services().motion.get_event(event_id)
    if not event:
        abort(404)
    return jsonify(event)


@motion_bp.route("/motion_event/<event_id>/preview.jpg")
@require_auth()
def motion_event_preview(event_id: str):
    if not EVENT_ID_PATTERN.fullmatch(event_id):
        abort(404)
    event_dir = _motion_root() / event_id
    preview_path = event_dir / "cover.jpg"
    if not preview_path.exists():
        preview_path = event_dir / "latest.jpg"
    preview_path = _resolve_motion_file(preview_path)
    return send_file(preview_path, mimetype="image/jpeg")


@motion_bp.route("/motion_event/<event_id>/<filename>")
@require_auth()
def motion_event_frame(event_id: str, filename: str):
    if not EVENT_ID_PATTERN.fullmatch(event_id):
        abort(404)
    if not EVENT_FRAME_PATTERN.fullmatch(filename):
        abort(404)
    event_dir = _motion_root() / event_id
    file_path = _resolve_motion_file(event_dir / filename)
    return send_file(file_path, mimetype="image/jpeg")


@motion_bp.route("/motion_capture/<filename>")
@require_auth()
def motion_capture(filename: str):
    if not LEGACY_CAPTURE_PATTERN.fullmatch(filename):
        abort(404)
    capture_path = _resolve_motion_file(_motion_root() / filename)
    return send_file(capture_path, mimetype="image/jpeg")
