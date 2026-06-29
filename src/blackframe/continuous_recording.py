"""Continuous loop recording with rotating MP4 segments.

Writes segments of fixed duration to a dedicated directory. Old segments are
deleted automatically when the total size exceeds the configured retention.
Each camera gets its own subdirectory keyed by camera_id.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from blackframe.recording import (
    _video_duration_sec,
    finalize_recording,
    open_mp4_writer,
    scaled_size,
)

logger = logging.getLogger(__name__)

SEGMENT_GLOB = "segment_*.mp4"

# Fallback bitrate when no real segment can be measured: mpeg4/mp4v at roughly
# 0.09 bits/pixel/frame. Continuous segments stay mp4v (see finalize_recording,
# transcode=False), so this constant is tuned for mp4v, not H.264.
MP4V_BITS_PER_PIXEL = 0.09


def _get_config(config: dict, key: str, default):
    return config.get(key, default) or default


def _observed_bitrate_bps(sample_dir: Path, sample_limit: int = 3) -> float | None:
    """Measured bits/sec from the most recent real segments, or None if not measurable.

    Sums encoded bytes and probed durations across up to ``sample_limit`` newest
    segments so the estimate reflects the actual scene/codec, not a guess.
    """
    if not sample_dir.exists():
        return None
    segments = sorted(
        sample_dir.glob(f"**/{SEGMENT_GLOB}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    total_bytes = 0
    total_seconds = 0.0
    for segment in segments[:sample_limit]:
        duration = _video_duration_sec(segment)
        if duration is None:
            continue
        try:
            total_bytes += segment.stat().st_size
        except OSError:
            continue
        total_seconds += duration
    if total_seconds <= 0 or total_bytes <= 0:
        return None
    return (total_bytes * 8) / total_seconds


def estimate_bitrate_bps(
    width: int,
    height: int,
    fps: float,
    sample_dir: "Path | str | None" = None,
) -> tuple[float, bool]:
    """Estimate encoded video bitrate (bits/sec) and whether it was calibrated.

    Calibrates from real segments under ``sample_dir`` when available; otherwise
    falls back to a documented mp4v constant. Returns ``(bitrate_bps, calibrated)``.
    """
    if sample_dir is not None:
        observed = _observed_bitrate_bps(Path(sample_dir))
        if observed is not None:
            return observed, True
    fps = max(1.0, float(fps))
    pixels = max(1, int(width) * int(height))
    return pixels * fps * MP4V_BITS_PER_PIXEL, False


class ContinuousRecorder:
    def __init__(self, camera_stream, config: dict, camera_id: str = "default"):
        self.camera = camera_stream
        self.config = config
        # camera_id is interpolated into the segment output path; restrict it to a
        # filesystem-safe charset so a profile id can never traverse out of the dir.
        safe_id = "".join(ch for ch in str(camera_id) if ch.isalnum() or ch in ("-", "_"))
        self.camera_id = safe_id or "default"
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_segment: Path | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("continuous_record_enabled"))

    def start(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._segment_loop,
                args=(self._stop,),
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self.lock:
            self._stop.set()
            self._thread = None
            self._current_segment = None

    def apply_config(self, config: dict) -> None:
        with self.lock:
            self.config = config
        if self.enabled:
            self.start()
        else:
            self.stop()

    def status(self) -> dict:
        with self.lock:
            active = self._thread is not None and self._thread.is_alive()
            seg = str(self._current_segment) if self._current_segment else None
        output_dir = self._output_dir()
        used_mb = self._dir_size_mb(output_dir) if output_dir.exists() else 0.0
        return {
            "enabled": self.enabled,
            "active": active,
            "current_segment": seg,
            "used_mb": round(used_mb, 1),
            "retain_hours": _get_config(self.config, "continuous_record_retain_hours", 3.0),
            "segment_min": _get_config(self.config, "continuous_record_segment_min", 10.0),
            "output_dir": str(self._output_dir()),
        }

    def _output_dir(self) -> Path:
        base = _get_config(self.config, "continuous_record_dir", "captures/continuous")
        return Path(base) / self.camera_id

    def _segment_duration_sec(self) -> float:
        return _get_config(self.config, "continuous_record_segment_min", 10.0) * 60.0

    def _max_retention_bytes(self) -> int:
        hours = _get_config(self.config, "continuous_record_retain_hours", 3.0)
        max_width = int(self.config.get("record_max_width", 0) or 0)
        # Coarse default frame size for the cold-start fallback (no segments yet);
        # once real segments exist, estimate_bitrate_bps calibrates from them.
        width = max_width or 1280
        height = int(width * 9 / 16)
        fps = _get_config(self.config, "record_fps", 10.0)
        bitrate_bps, _ = estimate_bitrate_bps(width, height, fps, sample_dir=self._output_dir())
        return int(bitrate_bps * hours * 3600 / 8)

    def _segment_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.enabled:
                time.sleep(5)
                continue

            frame = self.camera.get_raw_frame()
            if frame is None:
                time.sleep(1)
                continue

            output_dir = self._output_dir()
            output_dir.mkdir(parents=True, exist_ok=True)

            fps = max(1.0, float(_get_config(self.config, "record_fps", 10) or 10))
            size = scaled_size(frame, int(self.config.get("record_max_width", 0) or 0))
            segment_path = output_dir / f"segment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

            writer = self._open_writer(segment_path, fps, size)
            if writer is None:
                time.sleep(5)
                continue

            with self.lock:
                self._current_segment = segment_path

            seg_duration = self._segment_duration_sec()
            interval = 1.0 / fps
            started_at = time.time()
            next_frame_at = started_at

            try:
                while not stop.is_set() and self.enabled:
                    if time.time() - started_at >= seg_duration:
                        break
                    raw = self.camera.get_raw_frame()
                    if raw is None:
                        time.sleep(interval)
                        continue
                    if (raw.shape[1], raw.shape[0]) != size:
                        raw = cv2.resize(raw, size, interpolation=cv2.INTER_AREA)
                    writer.write(raw)
                    next_frame_at += interval
                    sleep_for = next_frame_at - time.time()
                    if sleep_for > 0:
                        time.sleep(min(sleep_for, interval))
                    else:
                        next_frame_at = time.time()
            except Exception:
                logger.exception("Errore scrittura segmento continuo")
            finally:
                writer.release()
                # Bulk segments: faststart only (no transcode) to spare the mini PC CPU.
                finalize_recording(segment_path, transcode=False)

            with self.lock:
                self._current_segment = None

            try:
                self._rotate_segments(output_dir)
            except Exception:
                logger.exception("Errore rotazione segmenti")

    def _open_writer(self, path: Path, fps: float, size: tuple) -> cv2.VideoWriter | None:
        return open_mp4_writer(path, fps, size)

    def _rotate_segments(self, output_dir: Path) -> None:
        segments = sorted(output_dir.glob(SEGMENT_GLOB), key=lambda p: p.stat().st_mtime)
        max_bytes = self._max_retention_bytes()
        total = sum(p.stat().st_size for p in segments if p.exists())
        for seg in segments:
            if total <= max_bytes:
                break
            try:
                size = seg.stat().st_size
                seg.unlink()
                total -= size
                logger.debug("Rimosso segmento continuo: %s", seg.name)
            except OSError:
                pass

    @staticmethod
    def _dir_size_mb(path: Path) -> float:
        total = 0
        for f in path.glob("**/*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total / (1024 * 1024)
