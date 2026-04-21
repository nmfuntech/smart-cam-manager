import time

from auth import require_auth
from flask import Blueprint, Response, current_app, redirect, render_template, url_for


video_bp = Blueprint("video", __name__)


def get_services():
    return current_app.config["services"]


@video_bp.route("/")
@require_auth()
def index():
    services = get_services()
    active_profile_id = services.features.camera_profiles.get_active_profile_id()
    if active_profile_id:
        return redirect(url_for("video.camera_view", profile_id=active_profile_id))
    return render_template("viewer.html", active_profile_id=None)


@video_bp.route("/camera/<profile_id>")
@require_auth()
def camera_view(profile_id: str):
    services = get_services()
    profile = services.features.camera_profiles.get_profile(profile_id)
    if not profile:
        return redirect(url_for("video.index"))

    active_profile_id = services.features.camera_profiles.get_active_profile_id()
    if active_profile_id and profile_id != active_profile_id:
        return redirect(url_for("video.camera_view", profile_id=active_profile_id))

    return render_template("viewer.html", active_profile_id=active_profile_id or profile_id)


@video_bp.route("/video_feed")
@require_auth()
def video_feed():
    services = get_services()
    target_interval_sec = 1 / 20  # Cap MJPEG push rate to reduce browser-side buffering.

    def generate():
        last_sequence = -1
        last_emit_at = 0.0
        while True:
            frame, sequence = services.camera.get_frame_packet()
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

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@video_bp.route("/snapshot.jpg")
@require_auth()
def snapshot():
    frame = get_services().camera.get_frame()
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


@video_bp.route("/stream_diagnostics")
@require_auth(api=True)
def stream_diagnostics():
    return get_services().camera.get_diagnostics()
