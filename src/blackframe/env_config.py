"""Camera, stream and motion settings read from the environment (.env)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

import cv2

logger = logging.getLogger(__name__)


def configure_opencv_runtime() -> None:
    """Optionally cap OpenCV internal workers on small CPUs."""
    raw = os.getenv("OPENCV_NUM_THREADS", "").strip()
    if not raw:
        return
    try:
        cv2.setNumThreads(max(1, int(raw)))
        cv2.setUseOptimized(True)
    except (ValueError, AttributeError):
        logger.warning("OPENCV_NUM_THREADS non valido: %r", raw)


configure_opencv_runtime()

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
    classification_yolox_model_path = os.getenv(
        "CLASSIFICATION_YOLOX_MODEL_PATH", "models/yolox_tiny.onnx"
    ).strip()
    classification_yolox_input_size = int(os.getenv("CLASSIFICATION_YOLOX_INPUT_SIZE", "416"))
    classification_person_min_confidence = float(
        os.getenv("CLASSIFICATION_PERSON_MIN_CONFIDENCE", "0.35")
    )
    classification_pet_min_confidence = float(
        os.getenv("CLASSIFICATION_PET_MIN_CONFIDENCE", "0.15")
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
        "classification_yolox_model_path": classification_yolox_model_path,
        "classification_yolox_input_size": classification_yolox_input_size,
        "classification_person_min_confidence": classification_person_min_confidence,
        "classification_pet_min_confidence": classification_pet_min_confidence,
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
