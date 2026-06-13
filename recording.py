"""Per-event MP4 recording.

An EventRecorder is told when a motion event opens and closes. While active it
pulls raw frames from the camera stream at a target FPS in its own thread,
prepending a short pre-roll captured before the trigger, and writes them to
``event.mp4`` inside the event directory. Recording never runs on the motion
detection loop so it cannot drop detection frames.
"""

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

RECORDING_FILE_NAME = "event.mp4"

# Browsers only play H.264 in an HTML5 <video>; mp4v (MPEG-4 Part 2) is rejected.
# Try avc1 (H.264) first and fall back to mp4v if the FFmpeg build lacks an encoder.
_VIDEO_FOURCC_PREFERENCE = ("avc1", "mp4v")


def finalize_faststart(path: "Path | str") -> None:
    """Move the MP4 moov atom to the front (faststart) so browsers can stream it.

    OpenCV's VideoWriter writes moov at the end, which many browsers refuse to play
    over HTTP. Uses ffmpeg stream-copy (no re-encode) when available; otherwise no-op.
    """
    path = Path(path)
    if not path.is_file() or shutil.which("ffmpeg") is None:
        return
    tmp = path.with_suffix(".faststart.mp4")
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
            logger.warning(
                "Faststart fallito per %s: %s", path, result.stderr.decode(errors="ignore")
            )
    except Exception:
        tmp.unlink(missing_ok=True)
        logger.exception("Errore faststart per %s", path)


def scaled_size(frame, max_width: int) -> tuple[int, int]:
    """Target (width, height) for recording, downscaled to max_width (0 = no limit)."""
    width, height = frame.shape[1], frame.shape[0]
    if max_width and width > max_width:
        height = int(round(height * max_width / width))
        height -= height % 2  # keep even dimensions for H.264
        width = max_width
    return (width, max(2, height))


def open_mp4_writer(path: "Path | str", fps: float, size) -> "cv2.VideoWriter | None":
    """Open a VideoWriter preferring browser-playable H.264, falling back to mp4v."""
    path = str(path)
    for tag in _VIDEO_FOURCC_PREFERENCE:
        fourcc = cv2.VideoWriter_fourcc(*tag)
        writer = cv2.VideoWriter(path, fourcc, fps, size)
        if writer.isOpened():
            if tag != "avc1":
                logger.warning("Codec H.264 non disponibile, uso fallback %s per %s", tag, path)
            return writer
        writer.release()
    logger.warning("Impossibile aprire VideoWriter per %s", path)
    return None


class EventRecorder:
    def __init__(self, camera_stream, config: dict):
        self.camera = camera_stream
        self.config = config
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active_dir: Path | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("record_enabled"))

    def start_event(self, event_dir) -> None:
        if not self.enabled or event_dir is None:
            return
        event_dir = Path(event_dir)
        with self.lock:
            if (
                self._active_dir == event_dir
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return
            self._stop_locked()
            self._active_dir = event_dir
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._record,
                args=(event_dir, self._stop),
                daemon=True,
            )
            self._thread.start()

    def stop_event(self, on_complete=None) -> None:
        with self.lock:
            thread = self._thread
            path = self._active_dir / RECORDING_FILE_NAME if self._active_dir else None
            self._stop_locked()
        if on_complete is not None and thread is not None and path is not None:

            def _wait_and_notify():
                thread.join(timeout=30)
                if path.exists():
                    try:
                        on_complete(path)
                    except Exception:
                        logger.exception("Errore callback completamento registrazione")

            threading.Thread(target=_wait_and_notify, daemon=True).start()

    def _stop_locked(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._active_dir = None
        self._thread = None

    def _record(self, event_dir: Path, stop: threading.Event) -> None:
        fps = max(1.0, float(self.config.get("record_fps", 10) or 10))
        max_duration = float(self.config.get("record_max_duration_sec", 60) or 60)
        preroll_sec = float(self.config.get("record_preroll_sec", 2.0) or 0.0)
        interval = 1.0 / fps
        writer = None
        size = None
        try:
            preroll = [self._decode(jpeg) for jpeg in self.camera.get_preroll_jpegs(preroll_sec)]
            preroll = [frame for frame in preroll if frame is not None]

            started_at = time.time()
            next_frame_at = started_at
            while not stop.is_set():
                if time.time() - started_at >= max_duration:
                    break
                frame = self.camera.get_raw_frame()
                if frame is None:
                    time.sleep(interval)
                    continue
                if writer is None:
                    size = scaled_size(frame, int(self.config.get("record_max_width", 0) or 0))
                    writer = self._open_writer(event_dir, fps, size)
                    if writer is None:
                        return
                    for pre in preroll:
                        writer.write(self._fit(pre, size))
                    preroll = []
                writer.write(self._fit(frame, size))
                next_frame_at += interval
                sleep_for = next_frame_at - time.time()
                if sleep_for > 0:
                    time.sleep(min(sleep_for, interval))
                else:
                    next_frame_at = time.time()
        except Exception:
            logger.exception("Errore registrazione video evento")
        finally:
            if writer is not None:
                writer.release()
                # Rewrite with moov atom at the front so browsers can stream the clip.
                finalize_faststart(event_dir / RECORDING_FILE_NAME)

    def _open_writer(self, event_dir: Path, fps: float, size):
        event_dir.mkdir(parents=True, exist_ok=True)
        path = event_dir / RECORDING_FILE_NAME
        writer = open_mp4_writer(path, fps, size)
        if writer is None:
            return None
        return writer

    @staticmethod
    def _decode(jpeg: bytes):
        try:
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            return None

    @staticmethod
    def _fit(frame, size):
        if frame is None:
            return frame
        if (frame.shape[1], frame.shape[0]) != size:
            return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        return frame
