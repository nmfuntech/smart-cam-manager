import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import cv2
from flask import Flask

from blackframe.agent import AgentService, AgentTranscriptStore
from blackframe.auth import auth_bp, configure_auth
from blackframe.automation import (
    ActionDispatcher,
    AutomationEngine,
    DeviceRegistry,
    EventContext,
    load_rules,
)
from blackframe.classification import PersonPetClassifier
from blackframe.continuous_recording import ContinuousRecorder
from blackframe.motion_events import MotionEventStore
from blackframe.notifications import TelegramNotifier
from blackframe.recording import EventRecorder
from blackframe.routes import register_blueprints
from blackframe.runtime_config import RuntimeConfigManager
from blackframe.service_layer import (
    CameraProfileService,
    FeatureServices,
    NotificationService,
    PresetService,
    RecordingService,
    WifiService,
)
from blackframe.telegram_commands import TelegramCommandBot
from scripts.runtime_paths import configure_runtime_environment

configure_runtime_environment()
logger = logging.getLogger(__name__)

# Privacy-at-rest: surveillance footage is the most sensitive asset here. Force a
# restrictive process umask so every motion frame, clip and event dir is created
# private (0600 files / 0700 dirs) instead of world-readable (default 0644/0755).
# Secret stores already chmod themselves; this only tightens.
os.umask(0o077)

# Prefer low-delay RTSP options to reduce frame buffering latency.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0|analyzeduration;0"
)


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

    # Percent-encode credentials: a password containing '@', ':' or '/' (all valid
    # in Tapo passwords) would otherwise corrupt the URL and could redirect the
    # RTSP client to an attacker-chosen host (credential exfiltration).
    return (
        f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}/{stream_path}"
    )


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
    min_area = int(os.getenv("MOTION_MIN_AREA", "600"))
    threshold = int(os.getenv("MOTION_THRESHOLD", "35"))
    blur_size = int(os.getenv("MOTION_BLUR_SIZE", "5"))
    cooldown = float(os.getenv("MOTION_COOLDOWN", "3"))
    min_interval = float(os.getenv("MOTION_FRAME_INTERVAL", "0.12"))
    capture_interval = float(os.getenv("MOTION_CAPTURE_INTERVAL", "0.18"))
    max_area_ratio = float(os.getenv("MOTION_MAX_AREA_RATIO", "0.45"))
    warmup_frames = int(os.getenv("MOTION_WARMUP_FRAMES", "12"))
    trigger_frames = int(os.getenv("MOTION_TRIGGER_FRAMES", "3"))
    clear_frames = int(os.getenv("MOTION_CLEAR_FRAMES", "8"))
    background_alpha = float(os.getenv("MOTION_BACKGROUND_ALPHA", "0.03"))
    mog2_history = int(os.getenv("MOTION_MOG2_HISTORY", "500"))
    scale_width = int(os.getenv("MOTION_SCALE_WIDTH", "480"))
    morph_kernel = int(os.getenv("MOTION_MORPH_KERNEL", "3"))
    morph_dilate_iter = int(os.getenv("MOTION_MORPH_DILATE_ITER", "2"))
    global_change_ratio = float(os.getenv("MOTION_GLOBAL_CHANGE_RATIO", "0.5"))
    learning_rate = float(os.getenv("MOTION_LEARNING_RATE", "-1"))
    learning_rate_active = float(os.getenv("MOTION_LEARNING_RATE_ACTIVE", "0.0005"))
    save_frames = os.getenv("MOTION_SAVE_FRAMES", "true").lower() == "true"
    save_dir = os.getenv("MOTION_SAVE_DIR", "captures/motion")
    event_gap = float(os.getenv("MOTION_EVENT_GAP", "4.0"))
    max_event_duration = float(os.getenv("MOTION_EVENT_MAX_DURATION", "45.0"))
    retention_days = float(os.getenv("MOTION_RETENTION_DAYS", "14"))
    retention_max_mb = float(os.getenv("MOTION_RETENTION_MAX_MB", "5000"))
    retention_interval_sec = float(os.getenv("MOTION_RETENTION_INTERVAL_SEC", "3600"))
    record_enabled = os.getenv("RECORD_ENABLED", "false").lower() == "true"
    record_fps = float(os.getenv("RECORD_FPS", "10"))
    record_preroll_sec = float(os.getenv("RECORD_PREROLL_SEC", "2.0"))
    record_postroll_sec = float(os.getenv("RECORD_POSTROLL_SEC", "3.0"))
    record_max_duration_sec = float(os.getenv("RECORD_MAX_DURATION_SEC", "60"))
    record_max_width = int(os.getenv("RECORD_MAX_WIDTH", "1280"))
    continuous_record_enabled = os.getenv("CONTINUOUS_RECORD_ENABLED", "false").lower() == "true"
    continuous_record_segment_min = float(os.getenv("CONTINUOUS_RECORD_SEGMENT_MIN", "10"))
    continuous_record_retain_hours = float(os.getenv("CONTINUOUS_RECORD_RETAIN_HOURS", "3"))
    continuous_record_dir = os.getenv("CONTINUOUS_RECORD_DIR", "captures/continuous")
    notify_prefer_video = os.getenv("NOTIFY_PREFER_VIDEO", "true").lower() == "true"
    classification_enabled = os.getenv("CLASSIFICATION_ENABLED", "false").lower() == "true"
    classification_backend = os.getenv("CLASSIFICATION_BACKEND", "local").strip().lower()
    classification_min_confidence = float(os.getenv("CLASSIFICATION_MIN_CONFIDENCE", "0.55"))
    classification_sample_policy = (
        os.getenv("CLASSIFICATION_SAMPLE_POLICY", "event_cover").strip().lower()
    )
    classification_local_model_path = os.getenv(
        "CLASSIFICATION_LOCAL_MODEL_PATH", "models/person_pet.onnx"
    ).strip()
    classification_local_labels_path = os.getenv(
        "CLASSIFICATION_LOCAL_LABELS_PATH", "models/person_pet_labels.txt"
    ).strip()
    classification_detection_model_path = os.getenv(
        "CLASSIFICATION_DETECTION_MODEL_PATH", "models/ssd_mobilenet_v2_coco.pb"
    ).strip()
    classification_detection_config_path = os.getenv(
        "CLASSIFICATION_DETECTION_CONFIG_PATH", "models/ssd_mobilenet_v2_coco.pbtxt"
    ).strip()
    classification_detection_input_size = int(
        os.getenv("CLASSIFICATION_DETECTION_INPUT_SIZE", "300")
    )
    classification_crop_to_motion = (
        os.getenv("CLASSIFICATION_CROP_TO_MOTION", "true").lower() == "true"
    )
    classification_crop_padding = float(os.getenv("CLASSIFICATION_CROP_PADDING", "0.2"))
    classification_detect_person = (
        os.getenv("CLASSIFICATION_DETECT_PERSON", "true").lower() == "true"
    )
    classification_detect_pet = os.getenv("CLASSIFICATION_DETECT_PET", "true").lower() == "true"

    if blur_size % 2 == 0:
        blur_size += 1
    if morph_kernel % 2 == 0:
        morph_kernel += 1

    return {
        "enabled": enabled,
        "min_area": min_area,
        "threshold": threshold,
        "mog2_var_threshold": threshold,
        "blur_size": blur_size,
        "cooldown": cooldown,
        "min_interval": min_interval,
        "capture_interval": capture_interval,
        "max_area_ratio": max_area_ratio,
        "warmup_frames": warmup_frames,
        "trigger_frames": trigger_frames,
        "clear_frames": clear_frames,
        "background_alpha": background_alpha,
        "mog2_history": mog2_history,
        "scale_width": scale_width,
        "morph_kernel": morph_kernel,
        "morph_dilate_iter": morph_dilate_iter,
        "global_change_ratio": global_change_ratio,
        "learning_rate": learning_rate,
        "learning_rate_active": learning_rate_active,
        "save_frames": save_frames,
        "save_dir": save_dir,
        "event_gap": event_gap,
        "max_event_duration": max_event_duration,
        "retention_days": retention_days,
        "retention_max_mb": retention_max_mb,
        "retention_interval_sec": retention_interval_sec,
        "record_enabled": record_enabled,
        "record_fps": record_fps,
        "record_preroll_sec": record_preroll_sec,
        "record_postroll_sec": record_postroll_sec,
        "record_max_duration_sec": record_max_duration_sec,
        "record_max_width": record_max_width,
        "continuous_record_enabled": continuous_record_enabled,
        "continuous_record_segment_min": continuous_record_segment_min,
        "continuous_record_retain_hours": continuous_record_retain_hours,
        "continuous_record_dir": continuous_record_dir,
        "notify_prefer_video": notify_prefer_video,
        "classification_enabled": classification_enabled,
        "classification_backend": classification_backend,
        "classification_min_confidence": classification_min_confidence,
        "classification_sample_policy": classification_sample_policy,
        "classification_local_model_path": classification_local_model_path,
        "classification_local_labels_path": classification_local_labels_path,
        "classification_detection_model_path": classification_detection_model_path,
        "classification_detection_config_path": classification_detection_config_path,
        "classification_detection_input_size": classification_detection_input_size,
        "classification_crop_to_motion": classification_crop_to_motion,
        "classification_crop_padding": classification_crop_padding,
        "classification_detect_person": classification_detect_person,
        "classification_detect_pet": classification_detect_pet,
        "notify_tail_suppress_sec": max(
            float(os.getenv("NOTIFY_TAIL_SUPPRESS_SEC", "0") or 0),
            event_gap * 2,
            15.0,
        ),
        "notify_burst_sec": max(float(os.getenv("NOTIFY_BURST_SEC", "0") or 0), 20.0),
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

    snapshot_online_ms = parse_int("STREAM_SNAPSHOT_INTERVAL_ONLINE_MS", 700, 100)
    # Cadenza di encoding JPEG. Default: la più frequente tra ciò che serve al preroll
    # (1/record_fps, così la clip resta fluida) e all'anteprima live (snapshot). Encodare
    # più spesso di così è spreco CPU. Override esplicito con STREAM_ENCODE_INTERVAL_MS;
    # 0 = encoda ogni frame (comportamento storico).
    record_fps = parse_float("RECORD_FPS", 10.0, 1.0)
    encode_interval_ms = min(snapshot_online_ms, max(1, int(1000.0 / record_fps)))
    override_encode_ms = parse_int("STREAM_ENCODE_INTERVAL_MS", 0, 0)
    if override_encode_ms > 0:
        encode_interval_ms = override_encode_ms

    return {
        "open_timeout_sec": parse_float("RTSP_OPEN_TIMEOUT_SEC", 8.0, 1.0),
        "reconnect_backoff_max_sec": parse_float("RTSP_RECONNECT_BACKOFF_MAX_SEC", 15.0, 1.0),
        "snapshot_interval_online_ms": snapshot_online_ms,
        "snapshot_interval_offline_ms": parse_int("STREAM_SNAPSHOT_INTERVAL_OFFLINE_MS", 2500, 250),
        "backlog_skip_frames": parse_int("RTSP_BACKLOG_SKIP_FRAMES", 1, 0),
        "jpeg_quality": parse_int("STREAM_JPEG_QUALITY", 85, 40),
        "max_width": parse_int("STREAM_MAX_WIDTH", 0, 0),
        "preroll_seconds": parse_float("RECORD_PREROLL_SEC", 2.0, 0.0),
        "encode_interval_ms": encode_interval_ms,
    }


def build_default_camera_profile() -> dict:
    return {
        "id": "env-default",
        "name": os.getenv("TAPO_CAMERA_NAME", "Camera principale"),
        "wifi_ssid": os.getenv("TAPO_WIFI_SSID", ""),
        "host": os.getenv("TAPO_HOST", "192.168.1.50"),
        "rtsp_port": int(os.getenv("TAPO_RTSP_PORT", "554")),
        "stream_path": os.getenv("TAPO_STREAM_PATH", "stream1"),
        "username": os.getenv("TAPO_USERNAME", ""),
        "password": os.getenv("TAPO_PASSWORD", ""),
        "onvif_port": int(os.getenv("TAPO_ONVIF_PORT", "2020")),
        "onvif_username": (
            os.getenv("TAPO_ONVIF_USERNAME")
            or os.getenv("TAPO_CAMERA_ACCOUNT_USER")
            or os.getenv("TAPO_USERNAME", "")
        ),
        "onvif_password": (
            os.getenv("TAPO_ONVIF_PASSWORD")
            or os.getenv("TAPO_CAMERA_ACCOUNT_PASSWORD")
            or os.getenv("TAPO_PASSWORD", "")
        ),
        "move_speed": float(os.getenv("TAPO_MOVE_SPEED", "0.6")),
        "move_timeout": float(os.getenv("TAPO_MOVE_TIMEOUT", "0.35")),
        "notes": "",
    }


def profile_motion_dir(profile_id: str) -> str:
    safe = "".join(ch for ch in str(profile_id) if ch.isalnum() or ch in ("-", "_"))
    return str(Path("captures/motion") / (safe or "default"))


def rtsp_url_from_profile(profile: dict) -> str:
    user = profile.get("username", "")
    password = profile.get("password", "")
    host = profile.get("host", "")
    port = int(profile.get("rtsp_port", 554) or 554)
    stream_path = profile.get("stream_path", "stream1") or "stream1"
    # Percent-encode credentials so special characters cannot corrupt the URL or
    # redirect the RTSP client to another host (see get_rtsp_url).
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{stream_path}"


def onvif_config_from_profile(profile: dict) -> dict:
    return {
        "host": profile.get("host", ""),
        "port": int(profile.get("onvif_port", 2020) or 2020),
        "username": profile.get("onvif_username") or profile.get("username"),
        "password": profile.get("onvif_password") or profile.get("password"),
        "move_speed": float(profile.get("move_speed", 0.6) or 0.6),
        "move_timeout": float(profile.get("move_timeout", 0.35) or 0.35),
    }


def motion_config_for_profile(profile: dict) -> dict:
    config = get_motion_config()
    config["save_dir"] = profile_motion_dir(profile["id"])
    return config


class CameraStream:
    def __init__(self, rtsp_url: str, config: dict | None = None):
        stream_config = config or {}
        self.rtsp_url = rtsp_url
        self.open_timeout_sec = float(stream_config.get("open_timeout_sec", 8.0))
        self.reconnect_backoff_max_sec = float(stream_config.get("reconnect_backoff_max_sec", 15.0))
        self.snapshot_interval_online_ms = int(
            stream_config.get("snapshot_interval_online_ms", 700)
        )
        self.snapshot_interval_offline_ms = int(
            stream_config.get("snapshot_interval_offline_ms", 2500)
        )
        self.backlog_skip_frames = int(stream_config.get("backlog_skip_frames", 1))
        self.jpeg_quality = int(stream_config.get("jpeg_quality", 85))
        self.max_width = int(stream_config.get("max_width", 0))
        self.preroll_seconds = float(stream_config.get("preroll_seconds", 3.0))
        # Throttle dell'encoding JPEG: il reader legge i raw frame a piena velocità
        # (latenza bassa + il recorder cattura dai raw), ma encoda il JPEG live solo
        # a questa cadenza. Default = cadenza del preroll (1/record_fps) limitata
        # all'intervallo snapshot, così live-view e preroll restano serviti senza
        # encodare ogni frame della camera (spreco CPU sul mini PC).
        self.encode_interval_sec = max(
            0.0, float(stream_config.get("encode_interval_ms", 0)) / 1000.0
        )
        self._last_encode_at = 0.0
        self.capture = None
        self.frame = None
        self.frame_sequence = 0
        # Sequence dei raw frame: incrementa ad ogni frame letto (il recorder vi si
        # aggancia per catturare a piena cadenza), distinta da frame_sequence che
        # avanza solo quando si (ri)encoda il JPEG live.
        self.raw_sequence = 0
        self.raw_frame = None
        self.preroll = deque(maxlen=150)
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
        self._stopped = threading.Event()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self.lock:
            if self.capture is not None:
                self.capture.release()
            self.capture = None

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
            self.connection_state = "degraded" if self.last_success_at is not None else "offline"
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
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            raise RuntimeError(f"Impossibile aprire stream RTSP su {self._endpoint()}")
        return capture

    def _reader(self) -> None:
        while not self._stopped.is_set():
            try:
                if self.capture is None or not self.capture.isOpened():
                    try:
                        self.capture = self._open_capture()
                        self._reset_backoff()
                    except Exception:
                        logger.exception("Errore apertura stream video")
                        self._record_open_failure("Impossibile aprire stream RTSP")
                        time.sleep(self._consume_backoff())
                        continue

                if self.backlog_skip_frames > 0:
                    ok_grab = self.capture.grab()
                    if not ok_grab:
                        ok = False
                        frame = None
                    else:
                        for _ in range(self.backlog_skip_frames):
                            if not self.capture.grab():
                                break
                        ok, frame = self.capture.retrieve()
                else:
                    ok, frame = self.capture.read()
                if not ok:
                    if self.capture is not None:
                        self.capture.release()
                    self.capture = None
                    self._record_read_failure("Frame non ricevuto dal nodo video")
                    time.sleep(self._consume_backoff())
                    continue

                now = time.time()
                # Il raw frame è aggiornato ad ogni iterazione (il recorder cattura da
                # qui): nessuna copia, ``frame`` è un array nuovo ad ogni read e non
                # viene mutato in seguito; i getter copiano una sola volta per il
                # chiamante. Elimina una copia full-frame per iterazione.
                with self.lock:
                    self.raw_frame = frame
                    self.raw_sequence += 1
                    self.last_frame_at = now
                    self.last_success_at = now
                    self.last_error = ""
                    self.last_error_stage = ""
                    self.connection_state = "online"

                # JPEG live + preroll solo a cadenza: encodare ogni frame della camera
                # è lo spreco CPU principale (i consumer leggono molto più di rado).
                if (
                    self.encode_interval_sec <= 0
                    or (now - self._last_encode_at) >= self.encode_interval_sec
                ):
                    render_frame = frame
                    if self.max_width > 0:
                        frame_width = frame.shape[1]
                        if frame_width > self.max_width:
                            scale = self.max_width / float(frame_width)
                            resized_height = int(frame.shape[0] * scale)
                            render_frame = cv2.resize(
                                frame,
                                (self.max_width, resized_height),
                                interpolation=cv2.INTER_AREA,
                            )

                    ok, buffer = cv2.imencode(
                        ".jpg",
                        render_frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                    )
                    if not ok:
                        self._record_read_failure("Encoding JPEG fallito sul frame live")
                        continue
                    self._last_encode_at = now
                    with self.lock:
                        self.frame = buffer.tobytes()
                        self.frame_sequence += 1
                        self.preroll.append((now, self.frame, self.frame_sequence))
                        self._trim_preroll(now)
                self._reset_backoff()
            except Exception:
                logger.exception("Errore stream video")
                if self.capture is not None:
                    self.capture.release()
                self.capture = None
                self._record_read_failure("Errore lettura stream RTSP")
                time.sleep(self._consume_backoff())

    def get_frame(self) -> bytes | None:
        with self.lock:
            return self.frame

    def get_frame_packet(self) -> tuple[bytes | None, int]:
        with self.lock:
            return self.frame, self.frame_sequence

    @property
    def frame_size(self) -> tuple[int, int] | None:
        with self.lock:
            if self.raw_frame is None:
                return None
            return (self.raw_frame.shape[1], self.raw_frame.shape[0])

    def get_raw_frame(self):
        with self.lock:
            if self.raw_frame is None:
                return None
            return self.raw_frame.copy()

    def get_raw_frame_packet(self) -> tuple:
        """Return (raw_frame copy, raw_sequence) or (None, sequence).

        Usa ``raw_sequence`` (avanza ad ogni frame letto), non ``frame_sequence``
        (avanza solo all'encode JPEG), così il recorder cattura a piena cadenza
        anche quando l'encoding live è throttato.
        """
        with self.lock:
            if self.raw_frame is None:
                return None, self.raw_sequence
            return self.raw_frame.copy(), self.raw_sequence

    def _trim_preroll(self, now: float) -> None:
        cutoff = now - self.preroll_seconds
        while self.preroll and self.preroll[0][0] < cutoff:
            self.preroll.popleft()

    def get_frame_sequence(self) -> int:
        with self.lock:
            return self.frame_sequence

    def get_preroll_timed_packets(self, seconds: float) -> list[tuple[float, bytes, int]]:
        """Return recent JPEG frames with capture time and stream sequence."""
        if seconds <= 0:
            return []
        with self.lock:
            if not self.preroll:
                return []
            cutoff = self.preroll[-1][0] - seconds
            return [(ts, jpeg, seq) for ts, jpeg, seq in self.preroll if ts >= cutoff]

    def get_preroll_packets(self, seconds: float) -> list[tuple[bytes, int]]:
        """Return recent JPEG frames and their stream sequence within the time window."""
        if seconds <= 0:
            return []
        with self.lock:
            if not self.preroll:
                return []
            cutoff = self.preroll[-1][0] - seconds
            return [(jpeg, seq) for ts, jpeg, seq in self.preroll if ts >= cutoff]

    def get_preroll_jpegs(self, seconds: float) -> list[bytes]:
        """Return recent encoded JPEG frames within the requested time window."""
        return [jpeg for jpeg, _seq in self.get_preroll_packets(seconds)]

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
            "backlog_skip_frames": self.backlog_skip_frames,
            "jpeg_quality": self.jpeg_quality,
            "max_width": self.max_width,
        }
        return status

    def apply_runtime_config(self, updates: dict) -> None:
        relevant = {
            "TAPO_HOST",
            "TAPO_RTSP_PORT",
            "TAPO_STREAM_PATH",
            "TAPO_USERNAME",
            "TAPO_PASSWORD",
        }
        if not any(key in relevant for key in updates):
            return
        with self.lock:
            self.rtsp_url = get_rtsp_url()
            if self.capture is not None:
                self.capture.release()
            self.capture = None
            self.frame = None
            self.frame_sequence = 0
            self.raw_sequence = 0
            self.raw_frame = None
            self.connection_state = "connecting"
            self.last_error = "Config stream aggiornata, reconnessione in corso..."
            self.last_error_stage = ""
        self._reset_backoff()


def _env_int_config(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float_config(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


_PERMS_MARKER_NAME = ".perms_hardened_v1"


def harden_captures_permissions() -> None:
    """Best-effort: make existing footage private (0700 dirs / 0600 files).

    New writes are already private via the process umask; this retro-fixes any
    clips/dirs created before the umask was in effect (e.g. an upgraded install).
    A marker file skips the recursive chmod on later boots: the tree walk grows
    with the archive and on the mini PC costs real boot time for zero gain once
    the retrofit has run (new files are already born private).
    """
    candidates = [
        "captures",
        os.getenv("MOTION_SAVE_DIR", ""),
        os.getenv("CONTINUOUS_RECORD_DIR", ""),
    ]
    seen: set[Path] = set()
    for base in candidates:
        if not base:
            continue
        try:
            root = Path(base).resolve()
        except OSError:
            continue
        if root in seen or not root.exists():
            continue
        seen.add(root)
        marker = root / _PERMS_MARKER_NAME
        if marker.exists():
            continue
        try:
            os.chmod(root, 0o700)
            for path in root.rglob("*"):
                try:
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
                except OSError:
                    continue
            marker.touch()
            os.chmod(marker, 0o600)
        except OSError:
            logger.warning("Impossibile irrigidire i permessi di %s", root)


_onvif_xml_hardened = False


def _harden_onvif_xml_parser() -> None:
    """Lock down the zeep/lxml settings onvif-zeep uses to parse camera responses.

    The camera talks ONVIF/SOAP in cleartext on the LAN, so its XML responses are
    untrusted input. onvif-zeep builds its zeep client with forbid_external unset
    and xml_huge_tree=True, leaving the door open to XXE (local file disclosure,
    e.g. reading .env), SSRF via remote schema imports, and entity-expansion DoS.
    We patch the Settings it constructs to forbid DTDs/entities/external refs and
    to keep libxml2's expansion limits. Idempotent; safe if onvif is absent.
    """
    global _onvif_xml_hardened
    if _onvif_xml_hardened:
        return
    try:
        import onvif.client as onvif_client
        from zeep import Settings
    except Exception:
        return

    class _HardenedSettings(Settings):
        # onvif-zeep sets `settings.xml_huge_tree = True` *after* construction;
        # expose it as a read-only False so that assignment is a no-op.
        @property
        def xml_huge_tree(self):
            return False

        @xml_huge_tree.setter
        def xml_huge_tree(self, value):
            pass

    def _hardened_settings_factory(*args, **kwargs):
        kwargs.setdefault("forbid_external", True)
        kwargs.setdefault("forbid_dtd", True)
        kwargs.setdefault("forbid_entities", True)
        return _HardenedSettings(*args, **kwargs)

    onvif_client.Settings = _hardened_settings_factory
    _onvif_xml_hardened = True


def _build_onvif_transport():
    """A zeep transport that refuses to fetch remote WSDL/XSD documents (SSRF guard).

    ONVIF WSDLs ship locally; the only remote references are http:// schema imports
    (e.g. w3.org/xmlsoap). Blocking remote *document loads* stops a MITM or a
    malicious schema from being pulled into the parser, while leaving the SOAP
    calls to the camera (transport.post) working normally.
    """
    from zeep.transports import Transport

    class _NoRemoteLoadTransport(Transport):
        def load(self, url):
            if str(url).lower().startswith(("http://", "https://")):
                raise RuntimeError(
                    f"Caricamento documento ONVIF remoto bloccato (anti-SSRF): {url}"
                )
            return super().load(url)

    return _NoRemoteLoadTransport()


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

            # The camera is untrusted input over a cleartext LAN protocol: harden
            # the SOAP/XML parser (XXE/SSRF/DoS) and block remote document fetches.
            _harden_onvif_xml_parser()
            camera = ONVIFCamera(
                self.config["host"],
                self.config["port"],
                self.config["username"],
                self.config["password"],
                transport=_build_onvif_transport(),
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
        except Exception:
            logger.exception("Connessione ONVIF fallita")
            self.last_error = "Connessione ONVIF fallita"
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
            except Exception:
                logger.exception("Comando PTZ fallito")
                self.available = False
                self.last_error = "Comando PTZ fallito"
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
            except Exception:
                logger.exception("Stop PTZ fallito")
                self.available = False
                self.last_error = "Stop PTZ fallito"
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
            except Exception:
                logger.exception("Home PTZ fallito")
                self.available = False
                self.last_error = "Home PTZ fallito"
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
            if "TAPO_ONVIF_USERNAME" in updates:
                self.config["username"] = str(updates["TAPO_ONVIF_USERNAME"])
                reconnect = True
            if "TAPO_ONVIF_PASSWORD" in updates:
                self.config["password"] = str(updates["TAPO_ONVIF_PASSWORD"])
                reconnect = True
            if "TAPO_MOVE_SPEED" in updates:
                self.config["move_speed"] = float(updates["TAPO_MOVE_SPEED"])
            if "TAPO_MOVE_TIMEOUT" in updates:
                self.config["move_timeout"] = float(updates["TAPO_MOVE_TIMEOUT"])
            if reconnect:
                self.available = False
                self._setup()

    def probe_async(self) -> None:
        """Eagerly check ONVIF availability at startup and log the outcome.

        Runs in a daemon thread so a slow/unreachable camera does not block boot.
        """

        def _probe() -> None:
            with self.lock:
                self._setup()
            if self.available:
                logger.info(
                    "PTZ ONVIF disponibile su %s:%s",
                    self.config["host"],
                    self.config["port"],
                )
            else:
                logger.warning("PTZ ONVIF non disponibile: %s", self.last_error)

        threading.Thread(target=_probe, daemon=True).start()


class MotionDetector:
    def __init__(
        self,
        camera_stream: CameraStream,
        config: dict,
        notifier=None,
        recorder=None,
        automation=None,
        camera_id: str | None = None,
    ):
        self.camera_stream = camera_stream
        self.config = config
        self.event_store = MotionEventStore(config)
        self.classifier = PersonPetClassifier.from_config(config)
        self.notifier = notifier
        self.recorder = recorder
        self.automation = automation
        # Id del profilo camera sorgente: finisce in EventContext.source così le
        # regole di automazione con filtro `source:` possono scattare per camera.
        self.camera_id = camera_id
        self._classified_events: set[str] = set()
        self._notified_events: set[str] = set()
        self._finalized_events: set[str] = set()
        self._last_classified_notify_at: float | None = None
        self._last_notify_at: float | None = None
        self._CLIP_CLASSIFY_MAX_FRAMES = 24
        # Automazione a bassa latenza: scatta durante l'evento appena un frame
        # contiene un soggetto, senza attendere la chiusura/registrazione/transcodifica.
        self._automation_fired: set[str] = set()
        self._live_classify_attempts: dict[str, int] = {}
        self._LIVE_CLASSIFY_MAX = 12
        self._stopped = threading.Event()
        self.lock = threading.Lock()
        # Coda dei task pesanti (I/O su disco + inferenza ONNX): eseguiti su un thread
        # daemon separato così non vengono mai svolti sotto self.lock né sul thread di
        # detection. Il thread di detection resta reattivo e get_status() non si blocca
        # mai dietro un salvataggio o una classificazione. Task "critical" (apertura/
        # chiusura evento) non vengono mai scartati; i salvataggi frame intermedi sì,
        # se la coda è sovraccarica (backpressure).
        self._event_queue: list[tuple[bool, Callable[[], None]]] = []
        self._event_queue_lock = threading.Lock()
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
        self.last_capture_saved_at = None
        # Normalized (x, y, w, h) of the latest motion bbox, used to crop the
        # classified frame down to the moving subject. None until first motion.
        self._last_motion_rect_norm = None
        self._bg_subtractor = None
        self._kernel_open = None
        self._kernel_dilate = None
        self._needs_subtractor_rebuild = False
        self._build_subtractor()
        self._restore_last_capture()
        self._warn_if_classification_unready()
        self._event_thread = threading.Thread(
            target=self._event_worker_loop, daemon=True, name="motion-event-worker"
        )
        self._event_thread.start()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _warn_if_classification_unready(self) -> None:
        """Log a clear warning when classification is on but no usable model is present."""
        classifier = getattr(self, "classifier", None)
        if classifier is not None and classifier.enabled and not classifier.ready:
            logger.warning(
                "Classificazione abilitata (backend %s) ma il modello non è disponibile: "
                "gli eventi resteranno '%s'. Fornisci il modello (vedi "
                "CLASSIFICATION_*_MODEL_PATH, cartella models/) oppure imposta "
                "CLASSIFICATION_ENABLED=false.",
                classifier.backend_name,
                PersonPetClassifier.LABEL_UNKNOWN,
            )

    def _build_subtractor(self) -> None:
        """(Re)build the MOG2 subtractor and morphology kernels. Runs only on the _run thread."""
        self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=int(self.config.get("mog2_history", 500)),
            varThreshold=float(self.config.get("mog2_var_threshold", 35)),
            detectShadows=True,
        )
        k = int(self.config.get("morph_kernel", 3))
        if k % 2 == 0:
            k += 1
        self._kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self._kernel_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self.processed_frames = 0
        self._needs_subtractor_rebuild = False

    def _current_learning_rate(self) -> float:
        if self.motion_detected:
            # Almost frozen during an active event so a still subject is not absorbed.
            return float(self.config.get("learning_rate_active", 0.0005))
        return float(self.config.get("learning_rate", -1))

    def _preprocess(self, frame):
        w = int(self.config.get("scale_width", 0) or 0)
        if w and frame.shape[1] > w:
            h = int(frame.shape[0] * w / frame.shape[1])
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(
            gray,
            (self.config["blur_size"], self.config["blur_size"]),
            0,
        )

    def _detect_motion(self, frame):
        """Pixel-level motion detection. Returns (motion_now, largest_usable_area, frame_area).

        Pure detection: no lock, no streak state. Runs only on the _run thread because
        the MOG2 subtractor is stateful.
        """
        processed = self._preprocess(frame)
        frame_area = float(processed.shape[0] * processed.shape[1])

        fg = self._bg_subtractor.apply(processed, learningRate=self._current_learning_rate())
        # MOG2 marks foreground as 255 and shadows as 127; keep only solid foreground.
        mask = cv2.inRange(fg, 250, 255)

        # Global-change guard: a lighting/exposure shift lights up most of the frame.
        global_ratio = self.config.get("global_change_ratio", 0.5)
        if global_ratio > 0 and cv2.countNonZero(mask) > frame_area * global_ratio:
            return False, 0.0, frame_area

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel_open)
        dilate_iter = int(self.config.get("morph_dilate_iter", 2))
        if dilate_iter > 0:
            mask = cv2.dilate(mask, self._kernel_dilate, iterations=dilate_iter)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        largest_usable_area = 0.0
        largest_contour = None
        motion_now = False
        for contour in contours:
            area = cv2.contourArea(contour)
            if self._is_usable_motion_area(area, frame_area):
                if area > largest_usable_area:
                    largest_usable_area = area
                    largest_contour = contour
                motion_now = True
        if largest_contour is not None:
            # Store the bbox normalized to the processed frame so it maps onto a
            # full-res frame of any resolution (aspect ratio is preserved).
            ph, pw = processed.shape[:2]
            x, y, w, h = cv2.boundingRect(largest_contour)
            self._last_motion_rect_norm = (x / pw, y / ph, w / pw, h / ph)
        return motion_now, largest_usable_area, frame_area

    def _get_event_store(self) -> MotionEventStore:
        if not hasattr(self, "event_store") or self.event_store is None:
            self.event_store = MotionEventStore(self.config)
        return self.event_store

    def _start_event_recording(self, event_dir) -> None:
        if self.recorder is not None and self.recorder.enabled and event_dir is not None:
            self.recorder.start_event(event_dir)

    def _save_motion_frame(self, frame, timestamp: str) -> str | None:
        store = self._get_event_store()
        filepath, event_id = store.save_frame(frame, timestamp)
        self.last_event_id = event_id
        self.last_capture_saved_at = time.time()
        if event_id:
            self._start_event_recording(store.current_event_dir)
        # Classification is subordinate to motion: it only ever runs as part of a motion
        # event. The _run loop already short-circuits when motion is disabled; this guard
        # makes the dependency explicit so classification can never run on its own.
        classification_on = (
            bool(self.config.get("enabled", True))
            and hasattr(self, "classifier")
            and self.classifier is not None
            and self.classifier.enabled
        )
        if event_id and classification_on:
            self._classify_event_frame(event_id, frame)
        return filepath

    def _prefer_video_notify(self) -> bool:
        return (
            self.recorder is not None
            and self.recorder.enabled
            and bool(self.config.get("notify_prefer_video", True))
        )

    def _already_notified(self, event_id: str) -> bool:
        """Dedup notifications: in-memory fast path + on-disk marker surviving restarts."""
        if event_id in self._notified_events:
            return True
        store = self._get_event_store()
        if store.event_was_notified(event_id):
            return True
        event_dir = store.find_event_dir(event_id)
        if event_dir is not None and event_dir.name != event_id:
            return event_dir.name in self._notified_events or store.event_was_notified(
                event_dir.name
            )
        return False

    def _mark_notified(self, event_id: str) -> None:
        store = self._get_event_store()
        event_dir = store.find_event_dir(event_id)
        resolved_id = event_dir.name if event_dir is not None else event_id
        self._notified_events.add(resolved_id)
        if resolved_id != event_id:
            self._notified_events.add(event_id)
        store.mark_event_notified(resolved_id)

    def _note_classified_notification(self, class_label: str | None) -> None:
        if class_label in (
            PersonPetClassifier.LABEL_PERSONA,
            PersonPetClassifier.LABEL_PET,
        ):
            self._last_classified_notify_at = time.monotonic()

    def _note_notification_sent(self) -> None:
        self._last_notify_at = time.monotonic()

    def _is_within_notify_burst(self) -> bool:
        last = getattr(self, "_last_notify_at", None)
        if last is None:
            return False
        return (time.monotonic() - last) < float(self.config.get("notify_burst_sec", 20))

    def _is_tail_motion_notification(self, classification: dict | None) -> bool:
        """Movimento generico subito dopo un alert persona/pet (stesso passaggio)."""
        classification = classification or {}
        if classification.get("detected_label") in (
            PersonPetClassifier.LABEL_PERSONA,
            PersonPetClassifier.LABEL_PET,
        ):
            return False
        status = classification.get("classification_status")
        if status not in ("no_detection", "motion_only", "unknown"):
            return False
        window = float(self.config.get("notify_tail_suppress_sec", 15))
        last = getattr(self, "_last_classified_notify_at", None)
        return last is not None and (time.monotonic() - last) < window

    def _resolve_event_label(self, classification: dict | None) -> str:
        """Category used for the folder name / archive: the confidently recognized
        class (person/pet, even if 'ignored'), otherwise a generic 'movimento' tag."""
        detected = (classification or {}).get("detected_label")
        if detected in (PersonPetClassifier.LABEL_PERSONA, PersonPetClassifier.LABEL_PET):
            return detected
        return "movimento"

    def _notification_class_label(self, classification: dict | None) -> str | None:
        """Etichetta per Telegram: preferisce la categoria riconosciuta (detected_label)."""
        classification = classification or {}
        label = classification.get("detected_label") or classification.get("class_label")
        if label in (PersonPetClassifier.LABEL_UNKNOWN, "unknown", None, ""):
            return None
        return label

    def _make_event_complete_callback(
        self, event_id: str, label: str, classification: dict | None, should_notify: bool
    ):
        """Runs after the recorder finalizes (or abandons) the clip."""

        def _callback(video_path: Path | None):
            self._finalize_closed_event_notification(
                event_id,
                label,
                classification,
                should_notify=should_notify,
                video_path=video_path,
            )

        return _callback

    def _classify_best_from_event(self, event_dir: Path) -> dict | None:
        """Scansiona la clip evento e sceglie il miglior rilevamento persona/pet.

        Il primo fotogramma (preroll) spesso non contiene ancora il soggetto: classificare
        solo cover.jpg o il frame iniziale produce falsi ``no_detection``.

        Questo è il burst CPU peggiore sul mini PC (decode video + inferenze ONNX
        in serie), quindi è contenuto in tre modi: tetto di frame configurabile,
        stride (un frame classificato ogni due, ``grab()`` scarta l'altro senza
        decodifica completa) e uscita anticipata quando un soggetto è già stato
        trovato con confidenza alta — altri frame non cambierebbero l'esito.
        """
        if not self.classifier.enabled or not self.classifier.ready:
            return None
        max_frames = _env_int_config(
            "CLASSIFICATION_CLIP_MAX_FRAMES", getattr(self, "_CLIP_CLASSIFY_MAX_FRAMES", 24)
        )
        early_exit_conf = _env_float_config("CLASSIFICATION_EARLY_EXIT_CONF", 0.85)
        best: dict | None = None
        best_conf = -1.0
        clip = event_dir / "event.mp4"
        if clip.is_file():
            capture = cv2.VideoCapture(str(clip))
            try:
                for _ in range(max_frames):
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    result = self.classifier.classify(frame)
                    if result and result.get("classification_status") == "ok":
                        label = result.get("detected_label")
                        conf = float(result.get("confidence") or 0)
                        if (
                            label
                            in (
                                PersonPetClassifier.LABEL_PERSONA,
                                PersonPetClassifier.LABEL_PET,
                            )
                            and conf > best_conf
                        ):
                            best = result
                            best_conf = conf
                    if best is not None and best_conf >= early_exit_conf:
                        break
                    # Stride: salta il frame successivo senza decodificarlo.
                    capture.grab()
            finally:
                capture.release()
        if best is not None:
            return best
        cover = event_dir / "cover.jpg"
        if cover.is_file():
            frame = cv2.imread(str(cover))
            if frame is not None:
                return self.classifier.classify(frame)
        return None

    def _maybe_classify_closed_event(
        self, event_id: str, classification: dict | None
    ) -> tuple[dict | None, str]:
        """Classifica a chiusura scansionando la clip (non solo il primo frame)."""
        classification = dict(classification) if classification else {}
        if classification.get("detected_label") in (
            PersonPetClassifier.LABEL_PERSONA,
            PersonPetClassifier.LABEL_PET,
        ):
            return classification, self._resolve_event_label(classification)

        store = self._get_event_store()
        store.ensure_preview_image(event_id)
        event_dir = store.find_event_dir(event_id)
        if event_dir is None or not self.classifier.enabled or not self.classifier.ready:
            return classification, self._resolve_event_label(classification)

        result = self._classify_best_from_event(event_dir)
        if not result:
            return classification, self._resolve_event_label(classification)
        logger.info(
            "Classificazione evento %s: label=%s conf=%s status=%s",
            event_id,
            result.get("detected_label"),
            result.get("confidence"),
            result.get("classification_status"),
        )
        store.save_event_meta(event_id, {"classification": result})
        return result, self._resolve_event_label(result)

    def _finalize_closed_event_notification(
        self,
        event_id: str | None,
        label: str,
        classification: dict | None,
        *,
        should_notify: bool,
        video_path: Path | None = None,
    ) -> str | None:
        """Rename a closed event and send Telegram once it is visible in the archive."""
        if not event_id:
            return None
        store = self._get_event_store()
        store.ensure_preview_image(event_id)
        classification, label = self._maybe_classify_closed_event(event_id, classification)
        should_notify = self._should_notify_for(classification or {})
        resolved_id = store.rename_event_with_label(event_id, label)
        logger.info(
            "Evento finalizzato: %s label=%s notify=%s",
            resolved_id,
            label,
            should_notify,
        )
        self._emit_automation(resolved_id, label, video_path)
        if not should_notify or self.notifier is None:
            return resolved_id
        if not self._should_notify_for(classification):
            return resolved_id
        self._deliver_event_notification(resolved_id, classification, video_path)
        return resolved_id

    def _emit_automation(self, event_id: str | None, label: str, video_path: Path | None) -> None:
        automation = getattr(self, "automation", None)
        if automation is None or not event_id:
            return
        # Già scattata durante l'evento (percorso a bassa latenza): non rifare.
        if self._event_base_id(event_id) in self._automation_fired:
            return
        try:
            ctx = EventContext(
                event_id=event_id,
                category=label,
                # getattr: i test costruiscono detector via __new__ senza __init__.
                source=getattr(self, "camera_id", None),
                timestamp=time.time(),
                video_path=str(video_path) if video_path else None,
            )
            automation.emit(ctx)
        except Exception:
            logger.exception("Automation emit fallito per evento %s", event_id)

    def _deliver_event_notification(
        self,
        event_id: str,
        classification: dict | None,
        video_path: Path | None,
    ) -> None:
        """Send video when available and preferred; otherwise fall back to cover photo."""
        if self.notifier is None or not event_id or self._already_notified(event_id):
            return

        store = self._get_event_store()
        event_dir = store.find_event_dir(event_id)
        resolved_id = event_dir.name if event_dir is not None else event_id
        class_label = self._notification_class_label(classification)
        base_dir = event_dir or Path(self.config["save_dir"]) / resolved_id
        # Resolve the clip after any category rename — the callback may still hold
        # the pre-rename path, which no longer exists once the folder is suffixed.
        clip = base_dir / "event.mp4"

        if self._prefer_video_notify() and clip.is_file():
            try:
                if self.notifier.notify_event_video(
                    event_id=resolved_id,
                    class_label=class_label,
                    video_path=str(clip),
                    on_delivered=lambda eid=resolved_id: self._mark_notified(eid),
                ):
                    self._mark_notified(resolved_id)
                    self._note_classified_notification(class_label)
                    self._note_notification_sent()
                    return
            except Exception:
                logger.exception("Invio video Telegram fallito")

        cover = base_dir / "cover.jpg"
        image_path = str(cover) if cover.exists() else None
        try:
            if self.notifier.notify_event(
                event_id=resolved_id,
                class_label=class_label,
                image_path=image_path,
                on_delivered=lambda eid=resolved_id: self._mark_notified(eid),
            ):
                self._mark_notified(resolved_id)
                self._note_classified_notification(class_label)
                self._note_notification_sent()
        except Exception:
            logger.exception("Invio notifica evento fallito")

    def _notify_event(self, event_id: str, classification: dict | None) -> None:
        if self.notifier is None or not event_id:
            return
        if self._already_notified(event_id):
            return
        class_label = self._notification_class_label(classification)
        store = self._get_event_store()
        event_dir = store.find_event_dir(event_id)
        cover = (event_dir or Path(self.config["save_dir"]) / event_id) / "cover.jpg"
        image_path = str(cover) if cover.exists() else None
        resolved_id = event_dir.name if event_dir is not None else event_id
        try:
            self.notifier.notify_event(
                event_id=resolved_id,
                class_label=class_label,
                image_path=image_path,
                on_delivered=lambda eid=resolved_id: self._mark_notified(eid),
            )
        except Exception:
            logger.exception("Invio notifica evento fallito")

    # Outcomes that should NOT raise an alert: a person/pet of a disabled category
    # ("ignored"), or a detection too weak ("low_confidence"). Plain motion without
    # a recognized subject ("no_detection") still notifies — the motion pipeline
    # already filtered the event and it appears in the archive.
    _NON_NOTIFY_CLASSIFICATION_STATUSES = frozenset({"low_confidence", "ignored"})

    def _classification_allows_notify(self, status: str | None) -> bool:
        return status not in self._NON_NOTIFY_CLASSIFICATION_STATUSES

    def _should_notify_for(self, classification: dict | None) -> bool:
        """Whether an event may raise an alert, re-checked at notification time.

        Beyond the frozen status, it also re-applies the per-category filter against
        the *current* classifier targets: toggling a category off mid-event then
        suppresses an event that was classified while that category was still on.
        """
        classification = classification or {}
        if not self._classification_allows_notify(classification.get("classification_status")):
            return False
        if self._is_within_notify_burst():
            return False
        if self._is_tail_motion_notification(classification):
            return False
        detected = classification.get("detected_label")
        classifier = getattr(self, "classifier", None)
        if detected and classifier is not None and detected not in classifier.targets:
            return False
        return True

    def _read_event_classification(self, event_id: str | None) -> dict:
        if not event_id:
            return {}
        try:
            import json

            event_dir = self._get_event_store().find_event_dir(event_id)
            if event_dir is None:
                return {}
            meta_path = event_dir / "meta.json"
            if not meta_path.exists():
                return {}
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data.get("classification") or {}
        except Exception:
            return {}

    def _get_last_class_label(self, event_id: str | None) -> str | None:
        return self._read_event_classification(event_id).get("class_label")

    def _crop_to_motion(self, frame):
        """Crop the full-res frame to the latest motion bbox (with padding).

        Focusing the detector on the moving subject improves accuracy when the
        subject is small in the scene and trims work. Returns the original frame
        when cropping is disabled or no motion bbox is known yet.
        """
        if not self.config.get("classification_crop_to_motion", True):
            return frame
        rect = self._last_motion_rect_norm
        if rect is None:
            return frame
        h, w = frame.shape[:2]
        pad = float(self.config.get("classification_crop_padding", 0.2))
        nx, ny, nw, nh = rect
        x1 = int(max(0.0, nx - nw * pad) * w)
        y1 = int(max(0.0, ny - nh * pad) * h)
        x2 = int(min(1.0, nx + nw * (1.0 + pad)) * w)
        y2 = int(min(1.0, ny + nh * (1.0 + pad)) * h)
        if x2 <= x1 or y2 <= y1:
            return frame
        return frame[y1:y2, x1:x2]

    @staticmethod
    def _event_base_id(event_id: str | None) -> str:
        """Id evento senza il suffisso categoria (``__persona``), stabile tra rename."""
        if not event_id:
            return ""
        base, _, _ = event_id.partition(MotionEventStore.LABEL_SEPARATOR)
        return base

    def _fire_live_automation(self, event_id: str, detected_label: str) -> None:
        """Fa scattare l'automazione durante l'evento (una sola volta per evento)."""
        automation = getattr(self, "automation", None)
        if automation is None:
            return
        base_id = self._event_base_id(event_id)
        if base_id in self._automation_fired:
            return
        self._automation_fired.add(base_id)
        try:
            automation.emit(
                EventContext(
                    event_id=event_id,
                    category=detected_label,
                    source=getattr(self, "camera_id", None),
                    timestamp=time.time(),
                )
            )
            logger.info("Automazione live: %s (%s)", event_id, detected_label)
        except Exception:
            logger.exception("Automazione live fallita per evento %s", event_id)

    def _classify_event_frame(self, event_id: str, frame) -> None:
        # Classifica i frame salvati durante l'evento e, appena trova un soggetto,
        # accende subito l'automazione. Riprova sui frame successivi finché ottiene
        # solo 'no_detection' (la persona spesso entra dopo i primi frame), con un
        # tetto di tentativi per non classificare all'infinito un puro movimento.
        if event_id in self._classified_events:
            return
        if self.classifier.sample_policy != "event_cover":
            return
        result = self.classifier.classify(self._crop_to_motion(frame))
        if result is None:
            return
        detected = result.get("detected_label")
        is_subject = detected in (PersonPetClassifier.LABEL_PERSONA, PersonPetClassifier.LABEL_PET)
        store = self._get_event_store()
        attempts = self._live_classify_attempts.get(event_id, 0) + 1
        self._live_classify_attempts[event_id] = attempts
        # Persisti il primo esito (così meta esiste sempre); riscrivi solo per
        # "promuovere" un esito non-soggetto a soggetto, evitando write ridondanti.
        if attempts == 1 or is_subject:
            store.save_event_meta(event_id, {"classification": result})
        if is_subject:
            # Soggetto riconosciuto: blocca la classificazione e accendi subito.
            self._classified_events.add(event_id)
            self._fire_live_automation(event_id, detected)
            return
        if attempts >= self._LIVE_CLASSIFY_MAX:
            self._classified_events.add(event_id)
        # Alerts are sent only after the event closes so Telegram and the UI archive
        # stay aligned (.closed marker exists before we notify).

    def _should_save_active_event_frame(self, now: float) -> bool:
        if not self.motion_detected:
            return False
        if self.clear_streak >= self.config["clear_frames"]:
            return False
        if self.last_capture_saved_at is None:
            return True
        return now - self.last_capture_saved_at >= self.config.get(
            "capture_interval", self.config["min_interval"]
        )

    def _is_usable_motion_area(self, area: float, frame_area: float) -> bool:
        if area < self.config["min_area"]:
            return False
        max_ratio = self.config.get("max_area_ratio", 1.0)
        if max_ratio <= 0:
            return True
        return area <= frame_area * max_ratio

    def _restore_last_capture(self) -> None:
        event = self._get_event_store().latest_event()
        if event:
            self.last_capture_path = event["preview_path"]
            self.last_event_id = event["id"]
            timestamp = event.get("timestamp")
            if timestamp is not None:
                self.last_motion_at_display = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    def stop(self) -> None:
        self._stopped.set()

    # --- worker eventi (I/O + classificazione fuori dal lock di detection) ---

    # Oltre questa profondità di coda i task non critici (salvataggi frame intermedi)
    # vengono scartati: su un mini PC sotto carico è meglio perdere un frame che far
    # crescere la coda senza limite. Apertura/chiusura evento restano sempre.
    _EVENT_QUEUE_MAX = 128

    def _enqueue_event(self, fn: Callable[[], None], *, critical: bool = False) -> None:
        with self._event_queue_lock:
            if not critical and len(self._event_queue) >= self._EVENT_QUEUE_MAX:
                logger.warning("Coda eventi piena: salvataggio frame scartato (backpressure)")
                return
            self._event_queue.append((critical, fn))

    def _event_worker_loop(self) -> None:
        while not self._stopped.is_set():
            with self._event_queue_lock:
                item = self._event_queue.pop(0) if self._event_queue else None
            if item is None:
                time.sleep(0.02)
                continue
            _critical, fn = item
            try:
                fn()
            except Exception:
                logger.exception("Task evento fallito sul worker")

    def _task_open_event(self, early_ts: str) -> None:
        store = self._get_event_store()
        early_id, early_dir = store.open_event(early_ts)
        if early_id:
            self.last_event_id = early_id
            logger.info("Evento movimento aperto: %s", early_id)
        self._start_event_recording(early_dir)

    def _task_open_and_save_frame(self, frame, timestamp: str) -> None:
        """Apre l'evento e salva il primo frame confermato nello stesso task."""
        self._task_open_event(timestamp)
        self._task_save_frame(frame, timestamp)

    def _task_save_frame(self, frame, timestamp: str) -> None:
        self.last_capture_path = self._save_motion_frame(frame, timestamp)

    def _task_close_event(self, closed_event_id: str | None) -> None:
        if not closed_event_id or closed_event_id in self._finalized_events:
            return
        self._finalized_events.add(closed_event_id)
        logger.info("Evento movimento in chiusura: %s", closed_event_id)
        closed_classification = self._read_event_classification(closed_event_id)
        store = self._get_event_store()
        store.close_current_event()
        # Every event must carry a label. With classification off there is no meta yet:
        # stamp a generic 'motion only' tag.
        if closed_event_id and not closed_classification:
            closed_classification = {
                "class_label": "movimento",
                "detected_label": None,
                "classification_status": "motion_only",
            }
            store.save_event_meta(closed_event_id, {"classification": closed_classification})
        label = self._resolve_event_label(closed_classification)
        classification_on = (
            bool(self.config.get("enabled", True))
            and self.classifier is not None
            and self.classifier.enabled
        )
        should_notify = True
        if classification_on:
            should_notify = self._should_notify_for(closed_classification)
        if self.recorder is not None and self.recorder.enabled:
            # Rename and notify happen in the callback, after the clip is finalized
            # (or after we know the clip is missing).
            self.recorder.stop_event(
                on_complete=self._make_event_complete_callback(
                    closed_event_id,
                    label,
                    closed_classification,
                    should_notify,
                )
            )
        else:
            self._finalize_closed_event_notification(
                closed_event_id,
                label,
                closed_classification,
                should_notify=should_notify,
            )
            if self.recorder is not None:
                self.recorder.stop_event()

    def _run(self) -> None:
        while not self._stopped.is_set():
            if not self.config["enabled"]:
                with self.lock:
                    self.motion_detected = False
                    self.last_error = "Rilevamento movimento disabilitato"
                    self.trigger_streak = 0
                    self.clear_streak = 0
                time.sleep(1)
                continue

            if self._needs_subtractor_rebuild:
                self._build_subtractor()

            frame, _frame_sequence = self.camera_stream.get_raw_frame_packet()
            if frame is None:
                time.sleep(self.config["min_interval"])
                continue

            try:
                motion_now, largest_usable_area, frame_area = self._detect_motion(frame)

                now = time.time()
                # Sotto il lock: SOLO la macchina a stati (contatori/flag letti da
                # get_status). Le azioni pesanti (apertura/chiusura evento, salvataggio
                # frame, classificazione, finalize+notifica) vengono raccolte qui come
                # closure e accodate al worker DOPO aver rilasciato il lock.
                deferred: list[tuple[bool, Callable[[], None]]] = []
                with self.lock:
                    self.processed_frames += 1
                    self.current_area = (
                        largest_usable_area
                        if largest_usable_area >= self.config["min_area"]
                        else 0.0
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
                            self.last_trigger_area = largest_usable_area
                            now_dt = datetime.now()
                            self.last_motion_at_display = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                            ts = now_dt.strftime("%Y%m%d_%H%M%S")
                            self.last_capture_saved_at = now
                            first_confirmed = self.trigger_streak == self.config["trigger_frames"]
                            if first_confirmed:
                                deferred.append(
                                    (
                                        True,
                                        lambda f=frame, t=ts: self._task_open_and_save_frame(f, t),
                                    )
                                )
                            else:
                                deferred.append(
                                    (
                                        False,
                                        lambda f=frame, t=ts: self._task_save_frame(f, t),
                                    )
                                )
                    else:
                        self.trigger_streak = 0
                        self.clear_streak += 1
                        if self._should_save_active_event_frame(now):
                            now_dt = datetime.now()
                            self.last_motion_at_display = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                            ts = now_dt.strftime("%Y%m%d_%H%M%S")
                            self.last_capture_saved_at = now
                            deferred.append(
                                (False, lambda f=frame, t=ts: self._task_save_frame(f, t))
                            )
                        enough_quiet = self.clear_streak >= self.config["clear_frames"]
                        out_of_cooldown = (
                            self.last_motion_at is None
                            or now - self.last_motion_at >= self.config["cooldown"]
                        )
                        has_open_event = self.last_event_id is not None and (
                            self.motion_detected
                            or self._get_event_store().current_event_dir is not None
                        )
                        if enough_quiet and out_of_cooldown and has_open_event:
                            closed_event_id = self.last_event_id
                            self.last_event_id = None
                            self.motion_detected = False
                            deferred.append(
                                (True, lambda eid=closed_event_id: self._task_close_event(eid))
                            )

                # Fuori dal lock: accoda i task pesanti al worker (FIFO, serializzati).
                for critical, fn in deferred:
                    self._enqueue_event(fn, critical=critical)
            except Exception:
                logger.exception("Errore motion detection")
                with self.lock:
                    self.last_error = "Errore motion detection"

            time.sleep(self.config["min_interval"])

    def get_status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.config["enabled"],
                "motion_detected": self.motion_detected,
                "last_motion_at": self.last_motion_at_display,
                "current_area": round(self.current_area, 2),
                "last_trigger_area": round(self.last_trigger_area, 2),
                "threshold": self.config["threshold"],
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
                    self.config["mog2_var_threshold"] = int(value)
                elif key == "MOTION_BLUR_SIZE":
                    self.config["blur_size"] = int(value)
                elif key == "MOTION_COOLDOWN":
                    self.config["cooldown"] = float(value)
                elif key == "MOTION_FRAME_INTERVAL":
                    self.config["min_interval"] = float(value)
                elif key == "MOTION_CAPTURE_INTERVAL":
                    self.config["capture_interval"] = float(value)
                elif key == "MOTION_MAX_AREA_RATIO":
                    self.config["max_area_ratio"] = float(value)
                elif key == "MOTION_WARMUP_FRAMES":
                    self.config["warmup_frames"] = int(value)
                elif key == "MOTION_TRIGGER_FRAMES":
                    self.config["trigger_frames"] = int(value)
                elif key == "MOTION_CLEAR_FRAMES":
                    self.config["clear_frames"] = int(value)
                elif key == "MOTION_BACKGROUND_ALPHA":
                    # Deprecated: legacy running-average alpha, no longer used by MOG2.
                    self.config["background_alpha"] = float(value)
                elif key == "MOTION_MOG2_HISTORY":
                    self.config["mog2_history"] = int(value)
                elif key == "MOTION_SCALE_WIDTH":
                    self.config["scale_width"] = int(value)
                elif key == "MOTION_MORPH_KERNEL":
                    self.config["morph_kernel"] = int(value)
                elif key == "MOTION_MORPH_DILATE_ITER":
                    self.config["morph_dilate_iter"] = int(value)
                elif key == "MOTION_GLOBAL_CHANGE_RATIO":
                    self.config["global_change_ratio"] = float(value)
                elif key == "MOTION_LEARNING_RATE":
                    self.config["learning_rate"] = float(value)
                elif key == "MOTION_LEARNING_RATE_ACTIVE":
                    self.config["learning_rate_active"] = float(value)
                elif key == "MOTION_SAVE_FRAMES":
                    self.config["save_frames"] = bool(value)
                elif key == "MOTION_SAVE_DIR":
                    self.config["save_dir"] = str(value)
                    self.event_store = MotionEventStore(self.config)
                    self.last_capture_path = None
                    self.last_event_id = None
                    self.last_motion_at = None
                    self.last_motion_at_display = None
                    self.last_capture_saved_at = None
                    self._classified_events.clear()
                    self._notified_events.clear()
                    self._restore_last_capture()
                elif key == "MOTION_EVENT_GAP":
                    self.config["event_gap"] = float(value)
                elif key == "MOTION_RETENTION_DAYS":
                    self.config["retention_days"] = float(value)
                elif key == "MOTION_RETENTION_MAX_MB":
                    self.config["retention_max_mb"] = float(value)
                elif key == "RECORD_ENABLED":
                    self.config["record_enabled"] = bool(value)
                elif key == "RECORD_FPS":
                    self.config["record_fps"] = float(value)
                elif key == "RECORD_PREROLL_SEC":
                    self.config["record_preroll_sec"] = float(value)
                elif key == "RECORD_POSTROLL_SEC":
                    self.config["record_postroll_sec"] = float(value)
                elif key == "RECORD_MAX_DURATION_SEC":
                    self.config["record_max_duration_sec"] = float(value)
                elif key == "RECORD_MAX_WIDTH":
                    self.config["record_max_width"] = int(value)
                elif key == "CONTINUOUS_RECORD_ENABLED":
                    self.config["continuous_record_enabled"] = bool(value)
                elif key == "CONTINUOUS_RECORD_SEGMENT_MIN":
                    self.config["continuous_record_segment_min"] = float(value)
                elif key == "CONTINUOUS_RECORD_RETAIN_HOURS":
                    self.config["continuous_record_retain_hours"] = float(value)
                elif key == "NOTIFY_PREFER_VIDEO":
                    self.config["notify_prefer_video"] = bool(value)
                elif key == "CLASSIFICATION_ENABLED":
                    self.config["classification_enabled"] = bool(value)
                elif key == "CLASSIFICATION_BACKEND":
                    self.config["classification_backend"] = str(value).strip().lower()
                elif key == "CLASSIFICATION_MIN_CONFIDENCE":
                    self.config["classification_min_confidence"] = float(value)
                elif key == "CLASSIFICATION_SAMPLE_POLICY":
                    self.config["classification_sample_policy"] = str(value).strip().lower()
                elif key == "CLASSIFICATION_LOCAL_MODEL_PATH":
                    self.config["classification_local_model_path"] = str(value).strip()
                elif key == "CLASSIFICATION_LOCAL_LABELS_PATH":
                    self.config["classification_local_labels_path"] = str(value).strip()
                elif key == "CLASSIFICATION_DETECTION_MODEL_PATH":
                    self.config["classification_detection_model_path"] = str(value).strip()
                elif key == "CLASSIFICATION_DETECTION_CONFIG_PATH":
                    self.config["classification_detection_config_path"] = str(value).strip()
                elif key == "CLASSIFICATION_DETECTION_INPUT_SIZE":
                    self.config["classification_detection_input_size"] = int(value)
                elif key == "CLASSIFICATION_CROP_TO_MOTION":
                    self.config["classification_crop_to_motion"] = bool(value)
                elif key == "CLASSIFICATION_CROP_PADDING":
                    self.config["classification_crop_padding"] = float(value)
                elif key == "CLASSIFICATION_DETECT_PERSON":
                    self.config["classification_detect_person"] = bool(value)
                elif key == "CLASSIFICATION_DETECT_PET":
                    self.config["classification_detect_pet"] = bool(value)

            classifier_changed = any(
                key
                in {
                    "CLASSIFICATION_ENABLED",
                    "CLASSIFICATION_BACKEND",
                    "CLASSIFICATION_MIN_CONFIDENCE",
                    "CLASSIFICATION_SAMPLE_POLICY",
                    "CLASSIFICATION_LOCAL_MODEL_PATH",
                    "CLASSIFICATION_LOCAL_LABELS_PATH",
                    "CLASSIFICATION_DETECTION_MODEL_PATH",
                    "CLASSIFICATION_DETECTION_CONFIG_PATH",
                    "CLASSIFICATION_DETECTION_INPUT_SIZE",
                    "CLASSIFICATION_DETECT_PERSON",
                    "CLASSIFICATION_DETECT_PET",
                }
                for key in updates
            )
            if classifier_changed:
                self.classifier = PersonPetClassifier.from_config(self.config)
                self._classified_events.clear()
                self._warn_if_classification_unready()

            reset_background = any(
                key
                in {
                    "MOTION_THRESHOLD",
                    "MOTION_BLUR_SIZE",
                    "MOTION_MIN_AREA",
                    "MOTION_FRAME_INTERVAL",
                    "MOTION_MOG2_HISTORY",
                    "MOTION_SCALE_WIDTH",
                    "MOTION_MORPH_KERNEL",
                }
                for key in updates
            )
            if reset_background:
                # Defer rebuild to the _run thread: MOG2 is stateful and applied there.
                self._needs_subtractor_rebuild = True
                self.processed_frames = 0
                self.trigger_streak = 0
                self.clear_streak = 0
                self.last_capture_saved_at = None

    def list_events(self, limit: int = 8, include_frames: bool = False) -> list[dict]:
        return self._get_event_store().list_events(
            limit=limit,
            include_frames=include_frames,
        )

    def get_event(self, event_id: str):
        return self._get_event_store().get_event(event_id)

    def clear_events(self) -> int:
        with self.lock:
            removed = self._get_event_store().clear_all()
            self.last_capture_path = None
            self.last_event_id = None
            self.last_motion_at = None
            self.last_motion_at_display = None
            self.last_trigger_area = 0.0
            self.current_area = 0.0
            self.last_capture_saved_at = None
            self.motion_detected = False
            self._classified_events.clear()
            self._notified_events.clear()
            self._finalized_events.clear()
            self._automation_fired.clear()
            self._live_classify_attempts.clear()
            self._last_classified_notify_at = None
            self._last_notify_at = None
            return removed

    def purge_old_events(self) -> int:
        days = float(self.config.get("retention_days", 0) or 0)
        max_mb = float(self.config.get("retention_max_mb", 0) or 0)
        if days <= 0 and max_mb <= 0:
            return 0
        return self._get_event_store().purge_old_events(days, max_mb)


class RetentionJanitor:
    """Daemon thread that periodically deletes old motion events to bound disk usage.

    Riceve un provider (non una lista fissa) così copre anche i monitor avviati
    dopo il boot via ``start_monitor``: ogni giro rilegge l'elenco corrente dei
    MotionDetector (camera attiva + monitor) e ripulisce le directory di tutti —
    prima solo la camera attiva veniva purgata e i monitor crescevano senza limite.
    """

    def __init__(self, motion_provider: Callable[[], list["MotionDetector"]]):
        self._provider = motion_provider
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _sweep(self) -> float:
        """Un giro di retention su tutti i detector; ritorna l'intervallo per il prossimo."""
        interval = 3600.0
        for motion in self._provider():
            interval = float(motion.config.get("retention_interval_sec", 3600) or 3600)
            try:
                removed = motion.purge_old_events()
                if removed:
                    logger.info(
                        "Retention (%s): rimossi %s eventi di movimento",
                        motion.camera_id or "default",
                        removed,
                    )
            except Exception:
                logger.exception("Errore retention eventi (%s)", motion.camera_id or "default")
        return interval

    def _run(self) -> None:
        # Run once shortly after boot, then on the configured interval.
        time.sleep(10)
        while True:
            interval = self._sweep()
            time.sleep(max(interval, 60))


# Chiavi env runtime -> chiavi lowercase della config del ContinuousRecorder.
_CONTINUOUS_KEY_MAP = {
    "CONTINUOUS_RECORD_ENABLED": "continuous_record_enabled",
    "CONTINUOUS_RECORD_SEGMENT_MIN": "continuous_record_segment_min",
    "CONTINUOUS_RECORD_RETAIN_HOURS": "continuous_record_retain_hours",
}


def _coerce_continuous_value(env_key: str, value):
    """Gli updates del PATCH arrivano raw dal client (bool/num/stringa): la
    config del recorder vuole tipi nativi."""
    if env_key == "CONTINUOUS_RECORD_ENABLED":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


@dataclass
class CameraRuntime:
    """Bundle of background workers for one monitored (non-active) camera profile."""

    profile_id: str
    camera: CameraStream
    motion: MotionDetector
    recorder: EventRecorder
    continuous: ContinuousRecorder | None = None
    ptz: PTZController | None = None

    def stop(self) -> None:
        self.motion.stop()
        self.camera.stop()
        if self.recorder is not None:
            self.recorder.stop_event()
        if self.continuous is not None:
            self.continuous.stop()


def build_camera_runtime(profile: dict, notifier=None, automation=None) -> CameraRuntime:
    camera = CameraStream(rtsp_url_from_profile(profile), get_stream_config())
    motion_config = motion_config_for_profile(profile)
    recorder = EventRecorder(camera, motion_config)
    continuous = ContinuousRecorder(camera, motion_config, camera_id=profile["id"])
    motion = MotionDetector(
        camera,
        motion_config,
        notifier=notifier,
        recorder=recorder,
        automation=automation,
        camera_id=profile["id"],
    )
    continuous.start()
    return CameraRuntime(
        profile_id=profile["id"],
        camera=camera,
        motion=motion,
        recorder=recorder,
        continuous=continuous,
    )


@dataclass
class AppServices:
    camera: CameraStream
    ptz: PTZController
    motion: MotionDetector
    features: FeatureServices
    runtime_config: RuntimeConfigManager
    monitors: dict = field(default_factory=dict)
    continuous: ContinuousRecorder | None = None
    telegram_commands: TelegramCommandBot | None = None
    automation_engine: AutomationEngine | None = None
    automation_registry: DeviceRegistry | None = None
    agent: AgentService | None = None
    # Transcript della chat web /agente: esiste anche ad agente spento, così
    # la history resta leggibile dalla UI.
    agent_transcript: AgentTranscriptStore = field(default_factory=AgentTranscriptStore)

    def camera_and_motion(self, profile_id: str):
        """Resolve the (camera, motion) pair for a profile: active main pair or a monitor."""
        active_id = self.features.camera_profiles.get_active_profile_id()
        if profile_id == active_id:
            return self.camera, self.motion
        runtime = self.monitors.get(profile_id)
        if runtime is None:
            return None, None
        return runtime.camera, runtime.motion

    def start_monitor(self, profile_id: str) -> bool:
        active_id = self.features.camera_profiles.get_active_profile_id()
        if profile_id == active_id or profile_id in self.monitors:
            return True
        profile = self.features.camera_profiles.get_profile(profile_id)
        if not profile:
            return False
        self.monitors[profile_id] = build_camera_runtime(
            profile, self.features.telegram, self.automation_engine
        )
        return True

    def stop_monitor(self, profile_id: str) -> None:
        runtime = self.monitors.pop(profile_id, None)
        if runtime is not None:
            runtime.stop()

    def apply_runtime_config_all(self, updates: dict) -> None:
        """Apply a runtime config change to the active camera AND every monitor.

        Each monitored camera runs its own MotionDetector/classifier, so a setting
        like the person/pet filter must reach them too or they keep using the old
        config (e.g. still notifying for a category disabled from Telegram/UI)."""
        self.camera.apply_runtime_config(updates)
        self.ptz.apply_runtime_config(updates)
        self.motion.apply_runtime_config(updates)
        for runtime in self.monitors.values():
            runtime.camera.apply_runtime_config(updates)
            runtime.motion.apply_runtime_config(updates)
            # monitored cameras have no PTZ controller
        self._apply_continuous_config(updates)

    def _apply_continuous_config(self, updates: dict) -> None:
        """Propaga i cambi CONTINUOUS_RECORD_* ai recorder in esecuzione.

        Senza questo, il toggle da UI scrive l'env ma il ContinuousRecorder
        attivo mantiene la config vecchia fino al riavvio (apply_config avvia/
        ferma il segment loop da solo)."""
        if not any(key in _CONTINUOUS_KEY_MAP for key in updates):
            return
        if self.continuous is not None:
            # L'env è già aggiornato da RuntimeConfigManager.update: la config
            # della camera attiva si può rileggere per intero.
            self.continuous.apply_config(get_motion_config())
        for runtime in self.monitors.values():
            if runtime.continuous is None:
                continue
            # La config dei monitor deriva dal profilo (save_dir/camera propri):
            # merge delle sole chiavi continuous, mai get_motion_config().
            merged = dict(runtime.continuous.config)
            for env_key, cfg_key in _CONTINUOUS_KEY_MAP.items():
                if env_key in updates:
                    merged[cfg_key] = _coerce_continuous_value(env_key, updates[env_key])
            runtime.continuous.apply_config(merged)

    def reload_automation(self) -> None:
        """Ricostruisce DeviceRegistry + AutomationEngine e li ri-aggancia a tutti i MotionDetector.

        Thread-safe: _automation_reload_lock serializza ricostruzioni concorrenti.
        L'assegnazione su MotionDetector è atomica in CPython (GIL).
        Il vecchio ActionDispatcher daemon drains silenziosamente senza stop esplicito.
        """
        with _automation_reload_lock:
            try:
                registry = _build_registry()
                engine = _build_automation()
                self.automation_registry = registry
                self.automation_engine = engine
                self.motion.automation = engine
                for runtime in self.monitors.values():
                    runtime.motion.automation = engine
                logger.info(
                    "Automazione ricaricata: %s · %d regole · %d device",
                    "attiva" if engine is not None else "disabilitata",
                    len(engine.rules) if engine is not None else 0,
                    len(registry.device_names()),
                )
            except Exception:
                logger.exception("Ricaricamento automazione fallito")

    def reload_agent(self) -> None:
        """Ricostruisce (o disattiva) il layer agentico dopo un toggle
        ``AGENT_ENABLED`` a runtime. Le eventuali proposte in attesa di
        conferma vengono perse — accettabile: l'utente può solo riproporle."""
        self.agent = _build_agent(self)
        logger.info("Agente ricaricato: %s", "attivo" if self.agent is not None else "disabilitato")


def _build_registry() -> DeviceRegistry:
    devices_path = os.getenv("AUTOMATION_DEVICES_PATH", "data/tuya_devices.json")
    return DeviceRegistry(store_path=devices_path)


_automation_reload_lock = threading.Lock()


def _build_automation() -> AutomationEngine | None:
    """Costruisce AutomationEngine + ActionDispatcher se AUTOMATION_ENABLED=true.

    Ritorna None se disabilitato o se il caricamento fallisce: il chiamante non
    deve mai crashare per un errore di configurazione dell'automazione.
    """
    if os.getenv("AUTOMATION_ENABLED", "false").lower() != "true":
        return None
    try:
        rules_path = os.getenv("AUTOMATION_RULES_PATH", "config/automation/rules.yaml")
        devices_path = os.getenv("AUTOMATION_DEVICES_PATH", "data/tuya_devices.json")
        registry = DeviceRegistry(store_path=devices_path)
        rules = load_rules(rules_path, known_devices=set(registry.device_names()))
        dispatcher = ActionDispatcher(registry)
        engine = AutomationEngine(rules, dispatcher=dispatcher)
        logger.info(
            "Automazione abilitata: %d regola/e, %d device",
            len(rules),
            len(registry.device_names()),
        )
        return engine
    except Exception:
        logger.exception("Avvio automazione fallito — layer disabilitato")
        return None


def _build_agent(services: "AppServices") -> AgentService | None:
    """Costruisce il layer agentico (NLU via Ollama) se AGENT_ENABLED=true.

    L'unica attivita' di rete alla costruzione e' il warm-up asincrono
    best-effort (thread daemon, mai bloccante) che precarica il modello in
    RAM: senza, il primo messaggio utente sul mini PC pagherebbe il
    caricamento da disco oltre AGENT_TIMEOUT_SEC. Se la variabile e'
    assente/false l'agente resta None e Telegram/Web rispondono "non
    abilitato" senza mai provare a contattare Ollama.
    """
    if os.getenv("AGENT_ENABLED", "false").lower() != "true":
        return None
    agent = AgentService(services)
    agent.start_warmup()
    return agent


def build_services() -> AppServices:
    harden_captures_permissions()
    runtime_config = RuntimeConfigManager()
    camera_profiles = CameraProfileService()
    camera_profiles.ensure_default_profile(build_default_camera_profile())
    active_profile_id = camera_profiles.get_active_profile_id()
    if active_profile_id:
        active_profile = camera_profiles.get_profile(active_profile_id)
        if active_profile:
            runtime_config.update(
                camera_profiles.build_runtime_updates(active_profile),
                allow_sensitive=True,
                allow_internal=True,
            )
    motion_config = get_motion_config()
    camera = CameraStream(get_rtsp_url(), get_stream_config())
    ptz = PTZController(get_onvif_config())
    ptz.probe_async()
    notifier = TelegramNotifier()
    recorder = EventRecorder(camera, motion_config)
    continuous = ContinuousRecorder(camera, motion_config, camera_id=active_profile_id or "default")
    automation_registry = _build_registry()
    automation = _build_automation()
    motion = MotionDetector(
        camera,
        motion_config,
        notifier=notifier,
        recorder=recorder,
        automation=automation,
        camera_id=active_profile_id or "default",
    )
    continuous.start()
    features = FeatureServices(
        presets=PresetService(),
        notifications=NotificationService(),
        recording=RecordingService(),
        telegram=notifier,
        camera_profiles=camera_profiles,
        wifi=WifiService(),
    )

    # Start background runtimes for any additional profiles flagged as monitored,
    # so they capture events and fire notifications even while the viewer shows
    # the active camera.
    monitors: dict[str, CameraRuntime] = {}
    try:
        for summary in camera_profiles.list_profiles():
            pid = summary.get("id")
            if not summary.get("monitored") or pid == active_profile_id:
                continue
            full_profile = camera_profiles.get_profile(pid)
            if full_profile:
                monitors[pid] = build_camera_runtime(full_profile, notifier, automation)
    except Exception:
        logger.exception("Avvio monitor multi-camera fallito")

    services = AppServices(
        camera=camera,
        ptz=ptz,
        motion=motion,
        features=features,
        runtime_config=runtime_config,
        monitors=monitors,
        continuous=continuous,
        automation_engine=automation,
        automation_registry=automation_registry,
    )
    # Dopo AppServices così il provider vede anche i monitor avviati a runtime.
    RetentionJanitor(lambda: [services.motion, *(rt.motion for rt in services.monitors.values())])
    services.telegram_commands = TelegramCommandBot(services)
    services.telegram_commands.start()
    services.agent = _build_agent(services)
    return services


def create_app(services: AppServices | None = None) -> Flask:
    app = Flask(__name__)
    configure_auth(app)
    app.config["services"] = services or build_services()
    app.register_blueprint(auth_bp)
    register_blueprints(app)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("APP_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        debug=False,
        use_reloader=False,
        threaded=True,
    )
