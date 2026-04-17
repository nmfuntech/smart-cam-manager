import time

from flask import Blueprint, Response, current_app, render_template


video_bp = Blueprint("video", __name__)


def get_services():
    return current_app.config["services"]


@video_bp.route("/")
def index():
    return render_template("index.html")


@video_bp.route("/video_feed")
def video_feed():
    def generate():
        while True:
            frame = get_services().camera.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@video_bp.route("/snapshot.jpg")
def snapshot():
    frame = get_services().camera.get_frame()
    if frame is None:
        return Response("Nessun frame disponibile", status=503, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")


@video_bp.route("/health")
def health():
    return {"status": "ok"}


@video_bp.route("/stream_status")
def stream_status():
    return get_services().camera.get_status()


@video_bp.route("/stream_diagnostics")
def stream_diagnostics():
    return get_services().camera.get_diagnostics()
