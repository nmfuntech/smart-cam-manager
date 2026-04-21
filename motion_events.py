import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import cv2


class MotionEventStore:
    CLOSED_MARKER_NAME = ".closed"

    def __init__(self, config: dict):
        self.config = config
        self.current_event_id = None
        self.current_event_dir = None
        self.current_event_last_at = None
        self.current_event_started_at = None
        self.current_event_frame_count = 0

    def save_frame(self, frame, timestamp: str) -> tuple[str | None, str | None]:
        if not self.config["save_frames"]:
            return None, None

        save_dir = Path(self.config["save_dir"])
        save_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        max_event_duration = float(self.config.get("max_event_duration", 0.0) or 0.0)
        duration_exceeded = (
            max_event_duration > 0
            and self.current_event_started_at is not None
            and now - self.current_event_started_at >= max_event_duration
        )
        if (
            self.current_event_dir is None
            or self.current_event_last_at is None
            or now - self.current_event_last_at > self.config["event_gap"]
            or duration_exceeded
        ):
            if duration_exceeded:
                self.close_current_event()
            self.current_event_id = f"motion_event_{timestamp}"
            self.current_event_dir = save_dir / self.current_event_id
            self.current_event_dir.mkdir(parents=True, exist_ok=True)
            self._marker_path(self.current_event_dir).unlink(missing_ok=True)
            self.current_event_started_at = now
            self.current_event_frame_count = 0

        self.current_event_last_at = now
        self.current_event_frame_count += 1
        filepath = (
            self.current_event_dir
            / f"frame_{timestamp}_{self.current_event_frame_count:03d}.jpg"
        )
        ok = cv2.imwrite(str(filepath), frame)
        if not ok:
            raise RuntimeError("Impossibile salvare il fotogramma di movimento")

        cover_path = self.current_event_dir / "cover.jpg"
        latest_path = self.current_event_dir / "latest.jpg"
        if not cover_path.exists():
            cv2.imwrite(str(cover_path), frame)
        cv2.imwrite(str(latest_path), frame)
        return str(filepath), self.current_event_id

    def latest_event(self):
        events = self.list_events(limit=1)
        return events[0] if events else None

    def close_current_event(self) -> None:
        if self.current_event_dir is not None:
            self._close_event_dir(self.current_event_dir)
        self.current_event_id = None
        self.current_event_dir = None
        self.current_event_last_at = None
        self.current_event_started_at = None
        self.current_event_frame_count = 0

    def get_event(self, event_id: str):
        for event in self.list_events(limit=500, include_frames=True):
            if event["id"] == event_id:
                return event
        return None

    def list_events(self, limit: int = 8, include_frames: bool = False) -> list[dict]:
        save_dir = Path(self.config["save_dir"])
        if not save_dir.exists():
            return []

        events = []
        events.extend(self._iter_saved_events(save_dir, include_frames))
        events.extend(self._iter_legacy_events(save_dir, include_frames))
        events = [event for event in events if event]
        events.sort(key=lambda event: event["timestamp"] or datetime.min, reverse=True)
        return events[:limit]

    def clear_all(self) -> int:
        save_dir = Path(self.config["save_dir"])
        if not save_dir.exists():
            return 0

        self.close_current_event()
        removed = 0
        for path in save_dir.iterdir():
            if path.is_dir() and path.name.startswith("motion_event_"):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            elif path.is_file() and path.name.startswith("motion_") and path.suffix == ".jpg":
                path.unlink(missing_ok=True)
                removed += 1

        return removed

    def _iter_saved_events(self, save_dir: Path, include_frames: bool):
        events = []
        for event_dir in sorted(save_dir.glob("motion_event_*"), reverse=True):
            if not event_dir.is_dir():
                continue
            if not self._ensure_event_is_closed(event_dir):
                continue
            event = self._build_saved_event(event_dir, include_frames)
            if event:
                events.append(event)
        return events

    def _iter_legacy_events(self, save_dir: Path, include_frames: bool):
        events = []
        legacy_group = []
        for path in sorted(save_dir.glob("motion_*.jpg")):
            ts = self._timestamp_from_name(path.name)
            if ts is None:
                continue
            if not legacy_group:
                legacy_group = [(path, ts)]
                continue
            prev_ts = legacy_group[-1][1]
            if (ts - prev_ts).total_seconds() <= self.config["event_gap"]:
                legacy_group.append((path, ts))
                continue
            events.append(self._build_legacy_event(legacy_group, include_frames))
            legacy_group = [(path, ts)]
        if legacy_group:
            events.append(self._build_legacy_event(legacy_group, include_frames))
        return events

    def _build_saved_event(self, event_dir: Path, include_frames: bool):
        cover_path = event_dir / "cover.jpg"
        latest_path = event_dir / "latest.jpg"
        frames = sorted(event_dir.glob("frame_*.jpg"))
        preview_path = cover_path if cover_path.exists() else latest_path
        if not preview_path.exists():
            return None
        event = {
            "id": event_dir.name,
            "label": event_dir.name.replace("motion_event_", ""),
            "path": str(event_dir),
            "preview_path": str(preview_path),
            "url": f"/motion_event/{event_dir.name}/preview.jpg",
            "frame_count": len(frames),
            "timestamp": self._timestamp_from_name(event_dir.name),
        }
        if include_frames:
            event["frames"] = [
                f"/motion_event/{event_dir.name}/{frame_path.name}"
                for frame_path in frames
            ] or [f"/motion_event/{event_dir.name}/preview.jpg"]
        return event

    def _build_legacy_event(self, grouped_paths, include_frames: bool = False):
        if not grouped_paths:
            return None
        _, first_ts = grouped_paths[0]
        last_path, _ = grouped_paths[-1]
        label = first_ts.strftime("%Y%m%d_%H%M%S")
        event = {
            "id": f"legacy_{label}",
            "label": label,
            "path": str(last_path),
            "preview_path": str(last_path),
            "url": f"/motion_capture/{last_path.name}",
            "frame_count": len(grouped_paths),
            "timestamp": first_ts,
        }
        if include_frames:
            event["frames"] = [
                f"/motion_capture/{path.name}" for path, _ in grouped_paths
            ]
        return event

    def _timestamp_from_name(self, name: str):
        match = re.search(r"(\d{8}_\d{6})", name)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            return None

    def _marker_path(self, event_dir: Path) -> Path:
        return event_dir / self.CLOSED_MARKER_NAME

    def _close_event_dir(self, event_dir: Path) -> None:
        if not event_dir.exists():
            return
        self._marker_path(event_dir).touch(exist_ok=True)

    def _ensure_event_is_closed(self, event_dir: Path) -> bool:
        marker_path = self._marker_path(event_dir)
        if marker_path.exists():
            return True
        is_current_event = self.current_event_dir is not None and event_dir == self.current_event_dir
        if not self._is_event_stale(event_dir):
            return False
        if is_current_event:
            self.close_current_event()
            return True
        self._close_event_dir(event_dir)
        return True

    def _is_event_stale(self, event_dir: Path) -> bool:
        newest_mtime = event_dir.stat().st_mtime
        for path in event_dir.glob("*.jpg"):
            newest_mtime = max(newest_mtime, path.stat().st_mtime)
        return time.time() - newest_mtime > float(self.config.get("event_gap", 0.0) or 0.0)
