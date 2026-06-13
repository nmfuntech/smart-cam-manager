import re
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_file

from auth import rate_limit, require_auth, require_csrf

motion_bp = Blueprint("motion", __name__)
EVENT_ID_PATTERN = re.compile(r"^motion_event_\d{8}_\d{6}$")
EVENT_FRAME_PATTERN = re.compile(r"^(cover|latest|frame_\d{8}_\d{6}_\d{3})\.jpg$")
LEGACY_CAPTURE_PATTERN = re.compile(r"^motion_\d{8}_\d{6}\.jpg$")
PUBLIC_RUNTIME_UPDATE_KEYS = {
    "MOTION_ENABLED",
    "MOTION_THRESHOLD",
    "MOTION_MIN_AREA",
    "MOTION_MOG2_HISTORY",
    "MOTION_GLOBAL_CHANGE_RATIO",
    "MOTION_MORPH_KERNEL",
    "CLASSIFICATION_ENABLED",
    "CLASSIFICATION_BACKEND",
    "CLASSIFICATION_MIN_CONFIDENCE",
    "CLASSIFICATION_SAMPLE_POLICY",
    "MOTION_RETENTION_DAYS",
    "MOTION_RETENTION_MAX_MB",
    "RECORD_ENABLED",
    "RECORD_MAX_WIDTH",
    "NOTIFY_TELEGRAM_ENABLED",
    "NOTIFY_PREFER_VIDEO",
    "CONTINUOUS_RECORD_ENABLED",
    "CONTINUOUS_RECORD_SEGMENT_MIN",
    "CONTINUOUS_RECORD_RETAIN_HOURS",
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


@motion_bp.route("/cam/<profile_id>/motion_status")
@require_auth(api=True)
def camera_motion_status(profile_id: str):
    _, motion = get_services().camera_and_motion(profile_id)
    if motion is None:
        abort(404)
    return motion.get_status()


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
                    "error": ("Parametro non consentito: " + ", ".join(invalid_keys)),
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


@motion_bp.post("/api/open_captures_folder")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("open-folder", limit=20, window_seconds=60, api=True)
def open_captures_folder():
    """Open the local clips folder in the OS file manager.

    Only works when the server runs on the same machine as the browser (the local
    surveillance use case). Opens the configured save_dir; no user input is used.
    """
    folder = _motion_root()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "darwin":
            cmd = ["open", str(folder)]
        elif sys.platform.startswith("win"):
            cmd = ["explorer", str(folder)]
        else:
            cmd = ["xdg-open", str(folder)]
        subprocess.Popen(cmd)
    except FileNotFoundError:
        return (
            jsonify({"ok": False, "error": "Gestore file non disponibile su questo sistema"}),
            500,
        )
    except Exception:
        current_app.logger.exception("Apertura cartella clip fallita")
        return jsonify({"ok": False, "error": "Impossibile aprire la cartella"}), 500
    return jsonify({"ok": True, "path": str(folder)})


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


@motion_bp.route("/motion_event/<event_id>/video.mp4")
@require_auth()
def motion_event_video(event_id: str):
    if not EVENT_ID_PATTERN.fullmatch(event_id):
        abort(404)
    video_path = _resolve_motion_file(_motion_root() / event_id / "event.mp4")
    # conditional=True enables HTTP Range requests so the player can seek.
    return send_file(video_path, mimetype="video/mp4", conditional=True)


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


@motion_bp.get("/api/disk_estimate")
@require_auth(api=True)
def disk_estimate():
    """Stima spazio disco per la registrazione continua.

    Query param: retain_hours (float, default 3).
    """
    try:
        retain_hours = float(request.args.get("retain_hours", 3))
        retain_hours = max(0.1, retain_hours)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "retain_hours non valido"}), 400

    services = get_services()
    fps = float(services.motion.config.get("record_fps", 10) or 10)
    size = services.camera.frame_size  # (width, height) or None
    width, height = size if size else (1920, 1080)

    # Empirical estimate: mp4v at ~0.1 bits/pixel/frame
    bits_per_pixel = 0.1
    bitrate_bps = width * height * fps * bits_per_pixel
    total_bits = bitrate_bps * retain_hours * 3600
    estimated_mb = total_bits / (8 * 1024 * 1024)

    return jsonify(
        {
            "ok": True,
            "estimated_mb": round(estimated_mb, 1),
            "fps": fps,
            "width": width,
            "height": height,
            "retain_hours": retain_hours,
        }
    )


@motion_bp.route("/motion_capture/<filename>")
@require_auth()
def motion_capture(filename: str):
    if not LEGACY_CAPTURE_PATTERN.fullmatch(filename):
        abort(404)
    capture_path = _resolve_motion_file(_motion_root() / filename)
    return send_file(capture_path, mimetype="image/jpeg")
