import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import cv2
from dotenv import load_dotenv
from flask import Flask
from motion_events import MotionEventStore
from runtime_config import RuntimeConfigManager
from routes import register_blueprints
from service_layer import (
    FeatureServices,
    NotificationService,
    PresetService,
    RecordingService,
)


load_dotenv()

# Force RTSP over TCP for more reliable LAN streaming.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"


def get_rtsp_url() -> str:
    username = os.getenv("TAPO_USERNAME")
    password = os.getenv("TAPO_PASSWORD")
    host = os.getenv("TAPO_HOST", "192.168.1.50")
    port = os.getenv("TAPO_RTSP_PORT", "554")
    stream_path = os.getenv("TAPO_STREAM_PATH", "stream1")

    if not username or not password:
        raise RuntimeError(
            "Credenziali mancanti. Imposta TAPO_USERNAME e TAPO_PASSWORD nel file .env"
        )

    return f"rtsp://{username}:{password}@{host}:{port}/{stream_path}"


def get_onvif_config() -> dict:
    host = os.getenv("TAPO_HOST", "192.168.1.50")
    port = int(os.getenv("TAPO_ONVIF_PORT", "2020"))
    username = (
        os.getenv("TAPO_ONVIF_USERNAME")
        or os.getenv("TAPO_CAMERA_ACCOUNT_USER")
        or os.getenv("TAPO_USERNAME")
    )
    password = (
        os.getenv("TAPO_ONVIF_PASSWORD")
        or os.getenv("TAPO_CAMERA_ACCOUNT_PASSWORD")
        or os.getenv("TAPO_PASSWORD")
    )
    move_speed = float(os.getenv("TAPO_MOVE_SPEED", "0.6"))
    move_timeout = float(os.getenv("TAPO_MOVE_TIMEOUT", "0.35"))

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "move_speed": move_speed,
        "move_timeout": move_timeout,
    }


def get_motion_config() -> dict:
    enabled = os.getenv("MOTION_ENABLED", "true").lower() == "true"
    min_area = int(os.getenv("MOTION_MIN_AREA", "6000"))
    threshold = int(os.getenv("MOTION_THRESHOLD", "35"))
    blur_size = int(os.getenv("MOTION_BLUR_SIZE", "31"))
    cooldown = float(os.getenv("MOTION_COOLDOWN", "5"))
    min_interval = float(os.getenv("MOTION_FRAME_INTERVAL", "0.4"))
    warmup_frames = int(os.getenv("MOTION_WARMUP_FRAMES", "12"))
    trigger_frames = int(os.getenv("MOTION_TRIGGER_FRAMES", "3"))
    clear_frames = int(os.getenv("MOTION_CLEAR_FRAMES", "6"))
    background_alpha = float(os.getenv("MOTION_BACKGROUND_ALPHA", "0.08"))
    save_frames = os.getenv("MOTION_SAVE_FRAMES", "true").lower() == "true"
    save_dir = os.getenv("MOTION_SAVE_DIR", "captures/motion")
    event_gap = float(os.getenv("MOTION_EVENT_GAP", "3.0"))

    if blur_size % 2 == 0:
        blur_size += 1

    return {
        "enabled": enabled,
        "min_area": min_area,
        "threshold": threshold,
        "blur_size": blur_size,
        "cooldown": cooldown,
        "min_interval": min_interval,
        "warmup_frames": warmup_frames,
        "trigger_frames": trigger_frames,
        "clear_frames": clear_frames,
        "background_alpha": background_alpha,
        "save_frames": save_frames,
        "save_dir": save_dir,
        "event_gap": event_gap,
    }


def get_stream_config() -> dict:
    def parse_float(name: str, default: float, minimum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(value, minimum)

    def parse_int(name: str, default: int, minimum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(value, minimum)

    return {
        "open_timeout_sec": parse_float("RTSP_OPEN_TIMEOUT_SEC", 8.0, 1.0),
        "reconnect_backoff_max_sec": parse_float(
            "RTSP_RECONNECT_BACKOFF_MAX_SEC", 15.0, 1.0
        ),
        "snapshot_interval_online_ms": parse_int(
            "STREAM_SNAPSHOT_INTERVAL_ONLINE_MS", 700, 100
        ),
        "snapshot_interval_offline_ms": parse_int(
            "STREAM_SNAPSHOT_INTERVAL_OFFLINE_MS", 2500, 250
        ),
    }


class CameraStream:
    def __init__(self, rtsp_url: str, config: dict | None = None):
        stream_config = config or {}
        self.rtsp_url = rtsp_url
        self.open_timeout_sec = float(stream_config.get("open_timeout_sec", 8.0))
        self.reconnect_backoff_max_sec = float(
            stream_config.get("reconnect_backoff_max_sec", 15.0)
        )
        self.snapshot_interval_online_ms = int(
            stream_config.get("snapshot_interval_online_ms", 700)
        )
        self.snapshot_interval_offline_ms = int(
            stream_config.get("snapshot_interval_offline_ms", 2500)
        )
        self.capture = None
        self.frame = None
        self.raw_frame = None
        self.last_frame_at = None
        self.last_success_at = None
        self.last_error = "In attesa del primo frame..."
        self.last_error_stage = ""
        self.connection_state = "connecting"
        self.open_attempts = 0
        self.open_failures = 0
        self.read_failures = 0
        self.reconnect_count = 0
        self.last_connect_attempt_at = None
        self.last_open_error_at = None
        self.last_read_error_at = None
        self.next_retry_in_seconds = 0.0
        self._retry_delay_seconds = 1.0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _endpoint(self) -> str:
        parsed = urlparse(self.rtsp_url)
        return f"{parsed.hostname}:{parsed.port or 554}"

    def _to_iso(self, timestamp: float | None) -> str | None:
        if timestamp is None:
            return None
        return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")

    def _consume_backoff(self) -> float:
        with self.lock:
            delay = min(self._retry_delay_seconds, self.reconnect_backoff_max_sec)
            self.next_retry_in_seconds = round(delay, 2)
            self._retry_delay_seconds = min(
                max(self._retry_delay_seconds * 2, 1.0),
                self.reconnect_backoff_max_sec,
            )
        return delay

    def _reset_backoff(self) -> None:
        with self.lock:
            self._retry_delay_seconds = 1.0
            self.next_retry_in_seconds = 0.0

    def _record_open_failure(self, message: str) -> None:
        with self.lock:
            self.open_failures += 1
            self.reconnect_count += 1
            self.last_error = message
            self.last_error_stage = "open"
            self.connection_state = "offline"
            self.last_open_error_at = time.time()

    def _record_read_failure(self, message: str) -> None:
        with self.lock:
            self.read_failures += 1
            self.reconnect_count += 1
            self.last_error = message
            self.last_error_stage = "read"
            self.connection_state = (
                "degraded" if self.last_success_at is not None else "offline"
            )
            self.last_read_error_at = time.time()

    def _open_capture(self) -> cv2.VideoCapture:
        with self.lock:
            self.connection_state = "connecting"
            self.last_connect_attempt_at = time.time()
            self.open_attempts += 1
            self.last_error = "Connessione RTSP in corso..."
            self.last_error_stage = ""

        capture = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        timeout_ms = int(self.open_timeout_sec * 1000)
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
        if not capture.isOpened():
            raise RuntimeError(f"Impossibile aprire stream RTSP su {self._endpoint()}")
        return capture

    def _reader(self) -> None:
        while True:
            try:
                if self.capture is None or not self.capture.isOpened():
                    try:
                        self.capture = self._open_capture()
                        self._reset_backoff()
                    except Exception as exc:
                        print(f"Errore open stream video: {exc}")
                        self._record_open_failure(str(exc))
                        time.sleep(self._consume_backoff())
                        continue

                ok, frame = self.capture.read()
                if not ok:
                    if self.capture is not None:
                        self.capture.release()
                    self.capture = None
                    self._record_read_failure("Frame non ricevuto dal nodo video")
                    time.sleep(self._consume_backoff())
                    continue

                ok, buffer = cv2.imencode(".jpg", frame)
                if not ok:
                    self._record_read_failure("Encoding JPEG fallito sul frame live")
                    continue

                with self.lock:
                    self.frame = buffer.tobytes()
                    self.raw_frame = frame.copy()
                    self.last_frame_at = time.time()
                    self.last_success_at = self.last_frame_at
                    self.last_error = ""
                    self.last_error_stage = ""
                    self.connection_state = "online"
                self._reset_backoff()
            except Exception as exc:
                print(f"Errore stream video: {exc}")
                if self.capture is not None:
                    self.capture.release()
                self.capture = None
                self._record_read_failure(str(exc))
                time.sleep(self._consume_backoff())

    def get_frame(self) -> bytes | None:
        with self.lock:
            return self.frame

    def get_raw_frame(self):
        with self.lock:
            if self.raw_frame is None:
                return None
            return self.raw_frame.copy()

    def get_status(self) -> dict:
        with self.lock:
            age_seconds = None
            if self.last_frame_at is not None:
                age_seconds = round(time.time() - self.last_frame_at, 2)
            connected = self.frame is not None and self.connection_state == "online"
            recovering = self.connection_state in {"connecting", "degraded"}
            return {
                "connected": connected,
                "frame_age_seconds": age_seconds,
                "error": self.last_error,
                "connection_state": self.connection_state,
                "last_error_stage": self.last_error_stage,
                "endpoint": self._endpoint(),
                "open_attempts": self.open_attempts,
                "open_failures": self.open_failures,
                "read_failures": self.read_failures,
                "reconnect_count": self.reconnect_count,
                "next_retry_in_seconds": self.next_retry_in_seconds,
                "snapshot_interval_ms": (
                    self.snapshot_interval_online_ms
                    if connected or recovering
                    else self.snapshot_interval_offline_ms
                ),
                "last_success_at": self._to_iso(self.last_success_at),
                "last_connect_attempt_at": self._to_iso(self.last_connect_attempt_at),
                "last_open_error_at": self._to_iso(self.last_open_error_at),
                "last_read_error_at": self._to_iso(self.last_read_error_at),
            }

    def get_diagnostics(self) -> dict:
        status = self.get_status()
        status["stream_config"] = {
            "open_timeout_sec": self.open_timeout_sec,
            "reconnect_backoff_max_sec": self.reconnect_backoff_max_sec,
            "snapshot_interval_online_ms": self.snapshot_interval_online_ms,
            "snapshot_interval_offline_ms": self.snapshot_interval_offline_ms,
        }
        return status

    def apply_runtime_config(self, updates: dict) -> None:
        relevant = {"TAPO_HOST", "TAPO_RTSP_PORT", "TAPO_STREAM_PATH"}
        if not any(key in relevant for key in updates):
            return
        with self.lock:
            self.rtsp_url = get_rtsp_url()
            if self.capture is not None:
                self.capture.release()
            self.capture = None
            self.frame = None
            self.raw_frame = None
            self.connection_state = "connecting"
            self.last_error = "Config stream aggiornata, reconnessione in corso..."
            self.last_error_stage = ""
        self._reset_backoff()


class PTZController:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.camera = None
        self.media_service = None
        self.ptz_service = None
        self.profile_token = None
        self.continuous_request = None
        self.stop_request = None
        self.last_error = "PTZ non inizializzato"
        self.available = False

    def _setup(self) -> None:
        if not self.config["username"] or not self.config["password"]:
            self.last_error = "Credenziali ONVIF mancanti nel file .env"
            self.available = False
            return

        try:
            from onvif import ONVIFCamera

            camera = ONVIFCamera(
                self.config["host"],
                self.config["port"],
                self.config["username"],
                self.config["password"],
            )
            media_service = camera.create_media_service()
            ptz_service = camera.create_ptz_service()
            profile = media_service.GetProfiles()[0]

            continuous_request = ptz_service.create_type("ContinuousMove")
            continuous_request.ProfileToken = profile.token

            stop_request = ptz_service.create_type("Stop")
            stop_request.ProfileToken = profile.token
            stop_request.PanTilt = True
            stop_request.Zoom = True

            self.camera = camera
            self.media_service = media_service
            self.ptz_service = ptz_service
            self.profile_token = profile.token
            self.continuous_request = continuous_request
            self.stop_request = stop_request
            self.last_error = ""
            self.available = True
        except ModuleNotFoundError:
            self.last_error = "Libreria ONVIF non installata. Installa onvif-zeep."
            self.available = False
        except Exception as exc:
            self.last_error = f"Connessione ONVIF fallita: {exc}"
            self.available = False

    def move(self, direction: str) -> tuple[bool, str]:
        with self.lock:
            if not self.available:
                self._setup()

            if not self.available:
                return False, self.last_error

            vector_map = {
                "left": (-1.0, 0.0),
                "right": (1.0, 0.0),
                "up": (0.0, 1.0),
                "down": (0.0, -1.0),
                "up_left": (-0.7, 0.7),
                "up_right": (0.7, 0.7),
                "down_left": (-0.7, -0.7),
                "down_right": (0.7, -0.7),
            }

            if direction not in vector_map:
                return False, "Direzione PTZ non supportata"

            try:
                x, y = vector_map[direction]
                speed = self.config["move_speed"]
                self.continuous_request.Velocity = {
                    "PanTilt": {
                        "x": x * speed,
                        "y": y * speed,
                    }
                }
                self.ptz_service.ContinuousMove(self.continuous_request)
                time.sleep(self.config["move_timeout"])
                self.ptz_service.Stop(self.stop_request)
                self.last_error = ""
                return True, ""
            except Exception as exc:
                self.available = False
                self.last_error = f"Comando PTZ fallito: {exc}"
                return False, self.last_error

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if not self.available:
                self._setup()

            if not self.available:
                return False, self.last_error

            try:
                self.ptz_service.Stop(self.stop_request)
                self.last_error = ""
                return True, ""
            except Exception as exc:
                self.available = False
                self.last_error = f"Stop PTZ fallito: {exc}"
                return False, self.last_error

    def home(self) -> tuple[bool, str]:
        with self.lock:
            if not self.available:
                self._setup()

            if not self.available:
                return False, self.last_error

            try:
                request_home = self.ptz_service.create_type("GotoHomePosition")
                request_home.ProfileToken = self.profile_token
                self.ptz_service.GotoHomePosition(request_home)
                self.last_error = ""
                return True, ""
            except Exception as exc:
                self.available = False
                self.last_error = f"Home PTZ fallito: {exc}"
                return False, self.last_error

    def get_status(self) -> dict:
        return {
            "available": self.available,
            "error": self.last_error,
            "host": self.config["host"],
            "port": self.config["port"],
        }

    def apply_runtime_config(self, updates: dict) -> None:
        with self.lock:
            reconnect = False
            if "TAPO_HOST" in updates:
                self.config["host"] = str(updates["TAPO_HOST"])
                reconnect = True
            if "TAPO_ONVIF_PORT" in updates:
                self.config["port"] = int(updates["TAPO_ONVIF_PORT"])
                reconnect = True
            if "TAPO_MOVE_SPEED" in updates:
                self.config["move_speed"] = float(updates["TAPO_MOVE_SPEED"])
            if "TAPO_MOVE_TIMEOUT" in updates:
                self.config["move_timeout"] = float(updates["TAPO_MOVE_TIMEOUT"])
            if reconnect:
                self.available = False
                self._setup()


class MotionDetector:
    def __init__(self, camera_stream: CameraStream, config: dict):
        self.camera_stream = camera_stream
        self.config = config
        self.event_store = MotionEventStore(config)
        self.lock = threading.Lock()
        self.background_frame = None
        self.processed_frames = 0
        self.trigger_streak = 0
        self.clear_streak = 0
        self.motion_detected = False
        self.last_motion_at = None
        self.last_motion_at_display = None
        self.current_area = 0.0
        self.last_trigger_area = 0.0
        self.last_capture_path = None
        self.last_event_id = None
        self.last_error = ""
        self._restore_last_capture()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _preprocess(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(
            gray,
            (self.config["blur_size"], self.config["blur_size"]),
            0,
        )

    def _get_event_store(self) -> MotionEventStore:
        if not hasattr(self, "event_store") or self.event_store is None:
            self.event_store = MotionEventStore(self.config)
        return self.event_store

    def _save_motion_frame(self, frame, timestamp: str) -> str | None:
        filepath, event_id = self._get_event_store().save_frame(frame, timestamp)
        self.last_event_id = event_id
        return filepath

    def _restore_last_capture(self) -> None:
        event = self._get_event_store().latest_event()
        if event:
            self.last_capture_path = event["preview_path"]
            self.last_event_id = event["id"]
            timestamp = event.get("timestamp")
            if timestamp is not None:
                self.last_motion_at_display = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    def _run(self) -> None:
        while True:
            if not self.config["enabled"]:
                with self.lock:
                    self.motion_detected = False
                    self.last_error = "Rilevamento movimento disabilitato"
                    self.trigger_streak = 0
                    self.clear_streak = 0
                time.sleep(1)
                continue

            frame = self.camera_stream.get_raw_frame()
            if frame is None:
                time.sleep(self.config["min_interval"])
                continue

            try:
                processed = self._preprocess(frame)

                if self.background_frame is None:
                    self.background_frame = processed.astype("float")
                    self.processed_frames = 1
                    time.sleep(self.config["min_interval"])
                    continue

                cv2.accumulateWeighted(
                    processed,
                    self.background_frame,
                    self.config["background_alpha"],
                )
                background_uint8 = cv2.convertScaleAbs(self.background_frame)
                delta = cv2.absdiff(background_uint8, processed)
                thresh = cv2.threshold(
                    delta,
                    self.config["threshold"],
                    255,
                    cv2.THRESH_BINARY,
                )[1]
                thresh = cv2.dilate(thresh, None, iterations=2)
                contours, _ = cv2.findContours(
                    thresh,
                    cv2.RETR_EXTERNAL,
                    cv2.CHAIN_APPROX_SIMPLE,
                )

                largest_area = 0.0
                motion_now = False
                for contour in contours:
                    area = cv2.contourArea(contour)
                    if area > largest_area:
                        largest_area = area
                    if area >= self.config["min_area"]:
                        motion_now = True

                now = time.time()
                with self.lock:
                    self.processed_frames += 1
                    self.current_area = (
                        largest_area if largest_area >= self.config["min_area"] else 0.0
                    )
                    self.last_error = ""
                    if self.processed_frames <= self.config["warmup_frames"]:
                        self.motion_detected = False
                        self.trigger_streak = 0
                        self.clear_streak = 0
                    elif motion_now:
                        self.trigger_streak += 1
                        self.clear_streak = 0
                        if self.trigger_streak >= self.config["trigger_frames"]:
                            self.motion_detected = True
                            self.last_motion_at = now
                            self.last_trigger_area = largest_area
                            now_dt = datetime.now()
                            self.last_motion_at_display = now_dt.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            capture_timestamp = now_dt.strftime("%Y%m%d_%H%M%S")
                            self.last_capture_path = self._save_motion_frame(
                                frame,
                                capture_timestamp,
                            )
                    else:
                        self.trigger_streak = 0
                        self.clear_streak += 1
                        enough_quiet = self.clear_streak >= self.config["clear_frames"]
                        out_of_cooldown = (
                            self.last_motion_at is None
                            or now - self.last_motion_at >= self.config["cooldown"]
                        )
                        if enough_quiet and out_of_cooldown:
                            self.motion_detected = False
            except Exception as exc:
                with self.lock:
                    self.last_error = f"Errore motion detection: {exc}"

            time.sleep(self.config["min_interval"])

    def get_status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.config["enabled"],
                "motion_detected": self.motion_detected,
                "last_motion_at": self.last_motion_at_display,
                "current_area": round(self.current_area, 2),
                "last_trigger_area": round(self.last_trigger_area, 2),
                "min_area": self.config["min_area"],
                "trigger_frames": self.config["trigger_frames"],
                "save_frames": self.config["save_frames"],
                "last_capture_path": self.last_capture_path,
                "last_event_id": self.last_event_id,
                "last_preview_url": "/latest_motion.jpg" if self.last_capture_path else None,
                "error": self.last_error,
            }

    def apply_runtime_config(self, updates: dict) -> None:
        with self.lock:
            for key, value in updates.items():
                if key == "MOTION_ENABLED":
                    self.config["enabled"] = bool(value)
                    if self.config["enabled"]:
                        self.last_error = ""
                    else:
                        self.last_error = "Rilevamento movimento disabilitato"
                        self.motion_detected = False
                        self.trigger_streak = 0
                        self.clear_streak = 0
                elif key == "MOTION_MIN_AREA":
                    self.config["min_area"] = int(value)
                elif key == "MOTION_THRESHOLD":
                    self.config["threshold"] = int(value)
                elif key == "MOTION_BLUR_SIZE":
                    self.config["blur_size"] = int(value)
                elif key == "MOTION_COOLDOWN":
                    self.config["cooldown"] = float(value)
                elif key == "MOTION_FRAME_INTERVAL":
                    self.config["min_interval"] = float(value)
                elif key == "MOTION_WARMUP_FRAMES":
                    self.config["warmup_frames"] = int(value)
                elif key == "MOTION_TRIGGER_FRAMES":
                    self.config["trigger_frames"] = int(value)
                elif key == "MOTION_CLEAR_FRAMES":
                    self.config["clear_frames"] = int(value)
                elif key == "MOTION_BACKGROUND_ALPHA":
                    self.config["background_alpha"] = float(value)
                elif key == "MOTION_SAVE_FRAMES":
                    self.config["save_frames"] = bool(value)
                elif key == "MOTION_SAVE_DIR":
                    self.config["save_dir"] = str(value)
                elif key == "MOTION_EVENT_GAP":
                    self.config["event_gap"] = float(value)

            reset_background = any(
                key
                in {
                    "MOTION_THRESHOLD",
                    "MOTION_BLUR_SIZE",
                    "MOTION_BACKGROUND_ALPHA",
                    "MOTION_MIN_AREA",
                    "MOTION_FRAME_INTERVAL",
                }
                for key in updates
            )
            if reset_background:
                self.background_frame = None
                self.processed_frames = 0
                self.trigger_streak = 0
                self.clear_streak = 0

    def list_events(self, limit: int = 8, include_frames: bool = False) -> list[dict]:
        return self._get_event_store().list_events(
            limit=limit,
            include_frames=include_frames,
        )

    def get_event(self, event_id: str):
        return self._get_event_store().get_event(event_id)


@dataclass
class AppServices:
    camera: CameraStream
    ptz: PTZController
    motion: MotionDetector
    features: FeatureServices
    runtime_config: RuntimeConfigManager


def build_services() -> AppServices:
    runtime_config = RuntimeConfigManager()
    camera = CameraStream(get_rtsp_url(), get_stream_config())
    ptz = PTZController(get_onvif_config())
    motion = MotionDetector(camera, get_motion_config())
    features = FeatureServices(
        presets=PresetService(),
        notifications=NotificationService(),
        recording=RecordingService(),
    )
    return AppServices(
        camera=camera,
        ptz=ptz,
        motion=motion,
        features=features,
        runtime_config=runtime_config,
    )


def create_app(services: AppServices | None = None) -> Flask:
    app = Flask(__name__)
    app.config["services"] = services or build_services()
    register_blueprints(app)
    return app

app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False, threaded=True)
