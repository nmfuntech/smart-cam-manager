"""Per-event MP4 recording.

An EventRecorder is told when a motion event opens and closes. While active it
pulls raw frames from the camera stream at a target FPS in its own thread,
prepending a short pre-roll captured before the trigger, and writes them to
``event.mp4`` inside the event directory. Recording never runs on the motion
detection loop so it cannot drop detection frames.
"""

import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

RECORDING_FILE_NAME = "event.mp4"


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

    def stop_event(self) -> None:
        with self.lock:
            self._stop_locked()

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
                    size = (frame.shape[1], frame.shape[0])
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

    def _open_writer(self, event_dir: Path, fps: float, size):
        event_dir.mkdir(parents=True, exist_ok=True)
        path = event_dir / RECORDING_FILE_NAME
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, fps, size)
        if not writer.isOpened():
            logger.warning("Impossibile aprire VideoWriter per %s", path)
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
