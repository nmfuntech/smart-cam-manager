import re
import time
from datetime import datetime
from pathlib import Path

import cv2


class MotionEventStore:
    def __init__(self, config: dict):
        self.config = config
        self.current_event_id = None
        self.current_event_dir = None
        self.current_event_last_at = None
        self.current_event_frame_count = 0

    def save_frame(self, frame, timestamp: str) -> tuple[str | None, str | None]:
        if not self.config["save_frames"]:
            return None, None

        save_dir = Path(self.config["save_dir"])
        save_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if (
            self.current_event_dir is None
            or self.current_event_last_at is None
            or now - self.current_event_last_at > self.config["event_gap"]
        ):
            self.current_event_id = f"motion_event_{timestamp}"
            self.current_event_dir = save_dir / self.current_event_id
            self.current_event_dir.mkdir(parents=True, exist_ok=True)
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

    def _iter_saved_events(self, save_dir: Path, include_frames: bool):
        events = []
        for event_dir in sorted(save_dir.glob("motion_event_*"), reverse=True):
            if not event_dir.is_dir():
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
        preview_path = latest_path if latest_path.exists() else cover_path
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
