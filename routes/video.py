import os
import threading
import time

from flask import Blueprint, Response, abort, current_app, redirect, render_template, url_for

from auth import require_auth

video_bp = Blueprint("video", __name__)

# Each MJPEG stream pins a worker thread in an infinite read loop. Cap the number
# of concurrent streams so an authenticated client cannot open many at once and
# exhaust CPU/threads on the single-worker process. Tune via APP_MAX_MJPEG_STREAMS.
_stream_lock = threading.Lock()
_active_streams = 0


def get_services():
    return current_app.config["services"]


def _max_streams() -> int:
    try:
        return max(1, int(os.getenv("APP_MAX_MJPEG_STREAMS", "8")))
    except ValueError:
        return 8


def _acquire_stream_slot() -> bool:
    global _active_streams
    with _stream_lock:
        if _active_streams >= _max_streams():
            return False
        _active_streams += 1
        return True


def _release_stream_slot() -> None:
    global _active_streams
    with _stream_lock:
        _active_streams = max(0, _active_streams - 1)


def _mjpeg_response(camera) -> Response:
    if not _acquire_stream_slot():
        return Response(
            "Troppi stream video attivi. Riprova piu tardi.",
            status=503,
            mimetype="text/plain",
        )
    target_interval_sec = 1 / 20  # Cap MJPEG push rate to reduce browser-side buffering.

    def generate():
        last_sequence = -1
        last_emit_at = 0.0
        try:
            while True:
                frame, sequence = camera.get_frame_packet()
                if frame is None:
                    time.sleep(0.1)
                    continue
                if sequence == last_sequence:
                    time.sleep(0.01)
                    continue

                now = time.time()
                wait_for = target_interval_sec - (now - last_emit_at)
                if wait_for > 0:
                    time.sleep(wait_for)

                last_sequence = sequence
                last_emit_at = time.time()

                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        finally:
            # Runs on client disconnect (GeneratorExit) so the slot is freed.
            _release_stream_slot()

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


def _resolve_camera(profile_id: str):
    camera, _ = get_services().camera_and_motion(profile_id)
    if camera is None:
        abort(404)
    return camera


@video_bp.route("/")
@require_auth()
def index():
    services = get_services()
    active_profile_id = services.features.camera_profiles.get_active_profile_id()
    if active_profile_id:
        return redirect(url_for("video.camera_view", profile_id=active_profile_id))
    return render_template(
        "viewer.html",
        active_profile_id=None,
        view_profile_id=None,
        is_active_view=True,
    )


@video_bp.route("/camera/<profile_id>")
@require_auth()
def camera_view(profile_id: str):
    services = get_services()
    profile = services.features.camera_profiles.get_profile(profile_id)
    if not profile:
        return redirect(url_for("video.index"))

    active_profile_id = services.features.camera_profiles.get_active_profile_id()
    is_active_view = profile_id == active_profile_id
    # The active camera gets the full viewer. A monitored (background) camera gets a
    # live-only view via its /cam/<id>/* endpoints. Anything else falls back to active.
    if not is_active_view and profile_id not in services.monitors:
        if active_profile_id:
            return redirect(url_for("video.camera_view", profile_id=active_profile_id))
        return redirect(url_for("video.index"))

    return render_template(
        "viewer.html",
        active_profile_id=active_profile_id or profile_id,
        view_profile_id=profile_id,
        is_active_view=is_active_view,
    )


@video_bp.route("/model-training")
@require_auth()
def model_training():
    return render_template("model_training.html")


@video_bp.route("/dashboard")
@require_auth()
def dashboard():
    services = get_services()
    profiles = services.features.camera_profiles.list_profiles()
    active_id = services.features.camera_profiles.get_active_profile_id()
    # A camera tile is live if it is the active one or has a running monitor runtime.
    cameras = [
        profile
        for profile in profiles
        if profile["id"] == active_id or profile["id"] in services.monitors
    ]
    return render_template("dashboard.html", cameras=cameras, active_profile_id=active_id)


@video_bp.route("/video_feed")
@require_auth()
def video_feed():
    return _mjpeg_response(get_services().camera)


@video_bp.route("/cam/<profile_id>/video_feed")
@require_auth()
def camera_video_feed(profile_id: str):
    return _mjpeg_response(_resolve_camera(profile_id))


@video_bp.route("/snapshot.jpg")
@require_auth()
def snapshot():
    frame = get_services().camera.get_frame()
    if frame is None:
        return Response("Nessun frame disponibile", status=503, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")


@video_bp.route("/cam/<profile_id>/snapshot.jpg")
@require_auth()
def camera_snapshot(profile_id: str):
    frame = _resolve_camera(profile_id).get_frame()
    if frame is None:
        return Response("Nessun frame disponibile", status=503, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")


@video_bp.route("/health")
def health():
    return {"status": "ok"}


@video_bp.route("/stream_status")
@require_auth(api=True)
def stream_status():
    return get_services().camera.get_status()


@video_bp.route("/cam/<profile_id>/stream_status")
@require_auth(api=True)
def camera_stream_status(profile_id: str):
    return _resolve_camera(profile_id).get_status()


@video_bp.route("/stream_diagnostics")
@require_auth(api=True)
def stream_diagnostics():
    return get_services().camera.get_diagnostics()
