"""Motion exclusion zones drawn by the user on the camera image.

Areas such as a window with moving sunlight, a TV, a mirror or a curtain fire
motion events without anything happening. The user draws polygons over the
live image (Impostazioni -> Zone); motion pixels inside them are ignored.

Storage: ``data/motion_zones.json`` keyed by camera id. Coordinates are
normalized to 0..1 so the same polygons work at any stream resolution and
at the reduced resolution used by the motion detector.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from blackframe.envutil import env_str

logger = logging.getLogger(__name__)

MAX_POLYGONS = 12
MAX_POINTS = 40

_lock = threading.Lock()


def _store_path() -> Path:
    return Path(env_str("MOTION_ZONES_PATH", "data/motion_zones.json"))


def _load_all() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def store_version() -> float:
    """Changes whenever the zones file changes (cheap poll for detectors)."""
    try:
        return _store_path().stat().st_mtime
    except OSError:
        return 0.0


def validate_polygons(raw) -> list[list[list[float]]]:
    """Return clean polygons or raise ValueError with an Italian message."""
    if not isinstance(raw, list):
        raise ValueError("Formato zone non valido")
    if len(raw) > MAX_POLYGONS:
        raise ValueError(f"Massimo {MAX_POLYGONS} zone per camera")
    polygons: list[list[list[float]]] = []
    for polygon in raw:
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= MAX_POINTS:
            raise ValueError(f"Ogni zona deve avere da 3 a {MAX_POINTS} punti")
        points: list[list[float]] = []
        for point in polygon:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Punto zona non valido")
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError) as exc:
                raise ValueError("Coordinate zona non numeriche") from exc
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("Coordinate zona fuori dall'immagine")
            points.append([round(x, 4), round(y, 4)])
        polygons.append(points)
    return polygons


def get_zones(camera_id: str) -> dict:
    entry = _load_all().get(camera_id) or {}
    try:
        exclude = validate_polygons(entry.get("exclude", []))
    except ValueError:
        logger.warning("Zone non valide per la camera %s: ignorate", camera_id)
        exclude = []
    return {"exclude": exclude}


def set_zones(camera_id: str, exclude) -> dict:
    from blackframe.service_layer import _write_private_text

    clean = validate_polygons(exclude)
    with _lock:
        data = _load_all()
        if clean:
            data[camera_id] = {"exclude": clean}
        else:
            data.pop(camera_id, None)
        _write_private_text(_store_path(), json.dumps(data, indent=2))
    return {"exclude": clean}


def build_mask(zones: dict, width: int, height: int) -> np.ndarray | None:
    """uint8 mask (255 = watched, 0 = ignored), or None when no zone is set."""
    polygons = zones.get("exclude") or []
    if not polygons:
        return None
    mask = np.full((height, width), 255, dtype=np.uint8)
    scale = np.array([width - 1, height - 1], dtype=np.float32)
    for polygon in polygons:
        points = np.round(np.array(polygon, dtype=np.float32) * scale).astype(np.int32)
        cv2.fillPoly(mask, [points], 0)
    return mask


class ZoneMaskCache:
    """Per-detector cache: rebuilds the mask only when the file or size changes."""

    def __init__(self, camera_id: str, check_interval_sec: float = 5.0) -> None:
        self.camera_id = camera_id
        self.check_interval_sec = check_interval_sec
        self._version: float | None = None
        self._shape: tuple[int, int] | None = None
        self._mask: np.ndarray | None = None
        self._watched_area: float = 0.0
        self._next_check = 0.0

    def get(self, shape: tuple[int, int], now: float) -> tuple[np.ndarray | None, float]:
        """(mask or None, watched pixel area) for a frame of ``shape`` (h, w)."""
        if now >= self._next_check:
            self._next_check = now + self.check_interval_sec
            version = store_version()
            if version != self._version:
                self._version = version
                self._shape = None  # force rebuild
        if self._shape != shape:
            self._shape = shape
            height, width = shape
            self._mask = build_mask(get_zones(self.camera_id), width, height)
            self._watched_area = (
                float(cv2.countNonZero(self._mask))
                if self._mask is not None
                else float(width * height)
            )
            if self._mask is not None:
                logger.info(
                    "Zone di esclusione attive (camera %s): %.0f%% dell'immagine sorvegliata",
                    self.camera_id,
                    100.0 * self._watched_area / max(1.0, float(width * height)),
                )
        return self._mask, self._watched_area
