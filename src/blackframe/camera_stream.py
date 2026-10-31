"""RTSP reader thread with backoff reconnect, live JPEG and pre-roll buffer."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

import cv2

from blackframe.env_config import get_rtsp_url

logger = logging.getLogger(__name__)


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
        self._raw_ready = threading.Condition(self.lock)
        self._stopped = threading.Event()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self.lock:
            if self.capture is not None:
                self.capture.release()
            self.capture = None
            self._raw_ready.notify_all()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)

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
                    except Exception as exc:
                        # Expected while the camera is offline: one line, no traceback
                        # (the repeat filter then collapses the reconnect loop).
                        logger.warning("Errore apertura stream video: %s", exc)
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
                    self._raw_ready.notify_all()
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

    def get_raw_frame_if_new(self, last_sequence: int) -> tuple:
        """Like get_raw_frame_packet, but skips the full-frame copy when the
        sequence has not advanced (returns ``(None, sequence)``)."""
        with self.lock:
            if self.raw_frame is None or self.raw_sequence == last_sequence:
                return None, self.raw_sequence
            return self.raw_frame.copy(), self.raw_sequence

    def wait_for_raw_frame(self, last_sequence: int, timeout: float) -> tuple:
        """Wait for a new frame without polling and copying unchanged arrays."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._raw_ready:
            while not self._stopped.is_set() and self.raw_sequence == last_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, last_sequence
                self._raw_ready.wait(timeout=remaining)
            if self.raw_frame is None or self.raw_sequence == last_sequence:
                return None, last_sequence
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
