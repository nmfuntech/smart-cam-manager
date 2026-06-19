import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import cv2


class MotionEventStore:
    CLOSED_MARKER_NAME = ".closed"
    META_FILE_NAME = "meta.json"
    # Separator between the timestamp and the category suffix in an event dir name,
    # e.g. motion_event_20260617_120000__persona. Double underscore so it never
    # collides with the single underscores inside the timestamp.
    LABEL_SEPARATOR = "__"
    EVENT_CATEGORIES = ("persona", "animale_domestico", "movimento")

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
            self.current_event_dir / f"frame_{timestamp}_{self.current_event_frame_count:03d}.jpg"
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

    def rename_event_with_label(self, event_id: str, label: str) -> str:
        """Append a category suffix to a closed event directory (e.g. ...__persona).

        Must be called only after the recorder has finished writing the event (the
        clip is finalized), never on the active event. Idempotent and best-effort:
        on any error or unknown label it leaves the directory untouched and returns
        the original event_id. The ``motion_event_`` prefix is preserved so globbing
        and retention keep working.
        """
        if not event_id or label not in self.EVENT_CATEGORIES:
            return event_id
        if self.LABEL_SEPARATOR in event_id:
            return event_id  # already labelled
        save_dir = Path(self.config["save_dir"])
        src = save_dir / event_id
        new_id = f"{event_id}{self.LABEL_SEPARATOR}{label}"
        dst = save_dir / new_id
        if not src.is_dir() or dst.exists():
            return event_id
        try:
            src.rename(dst)
        except OSError:
            return event_id
        return new_id

    def save_event_meta(self, event_id: str, data: dict) -> None:
        if not event_id or not isinstance(data, dict):
            return
        event_dir = Path(self.config["save_dir"]) / event_id
        if not event_dir.exists() or not event_dir.is_dir():
            return
        meta_path = event_dir / self.META_FILE_NAME
        existing = self._load_event_meta(event_dir) or {}
        existing.update(data)
        meta_path.write_text(
            json.dumps(existing, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

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

    def event_has_classification(self, event_id: str) -> bool:
        """Check on disk whether an event was already classified (dedup survives restart)."""
        if not event_id:
            return False
        event_dir = Path(self.config["save_dir"]) / event_id
        meta = self._load_event_meta(event_dir)
        return bool(meta.get("classification"))

    def event_was_notified(self, event_id: str) -> bool:
        """Check on disk whether a notification was already sent (dedup survives restart)."""
        if not event_id:
            return False
        event_dir = Path(self.config["save_dir"]) / event_id
        meta = self._load_event_meta(event_dir)
        return bool(meta.get("notified"))

    def mark_event_notified(self, event_id: str) -> None:
        """Persist that a notification was sent for this event."""
        self.save_event_meta(event_id, {"notified": True})

    def purge_old_events(self, max_age_days: float = 0.0, max_total_mb: float = 0.0) -> int:
        """Delete closed events older than max_age_days and/or beyond the max_total_mb quota.

        The currently-open event is never removed. Oldest events are removed first.
        A value of 0 (or less) disables that limit.
        """
        save_dir = Path(self.config["save_dir"])
        if not save_dir.exists():
            return 0

        entries = []
        for path in sorted(save_dir.glob("motion_event_*")):
            if not path.is_dir():
                continue
            if self.current_event_dir is not None and path == self.current_event_dir:
                continue
            ts = self._timestamp_from_name(path.name)
            event_time = ts.timestamp() if ts is not None else path.stat().st_mtime
            entries.append({"path": path, "time": event_time, "size": self._dir_size(path)})

        entries.sort(key=lambda item: item["time"])
        removed = 0

        if max_age_days and max_age_days > 0:
            cutoff = time.time() - max_age_days * 86400
            survivors = []
            for item in entries:
                if item["time"] < cutoff:
                    shutil.rmtree(item["path"], ignore_errors=True)
                    removed += 1
                else:
                    survivors.append(item)
            entries = survivors

        if max_total_mb and max_total_mb > 0:
            quota_bytes = max_total_mb * 1024 * 1024
            total = sum(item["size"] for item in entries)
            for item in entries:
                if total <= quota_bytes:
                    break
                shutil.rmtree(item["path"], ignore_errors=True)
                total -= item["size"]
                removed += 1

        return removed

    def _dir_size(self, path: Path) -> int:
        total = 0
        for child in path.glob("**/*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
        return total

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
        name = event_dir.name
        # Split the optional "__<category>" suffix off the timestamp for display.
        base, _, dir_category = name.partition(self.LABEL_SEPARATOR)
        event = {
            "id": name,
            "label": base.replace("motion_event_", ""),
            "category": dir_category or None,
            "path": str(event_dir),
            "preview_path": str(preview_path),
            "url": f"/motion_event/{name}/preview.jpg",
            "frame_count": len(frames),
            "timestamp": self._timestamp_from_name(name),
        }
        if (event_dir / "event.mp4").exists():
            event["video_url"] = f"/motion_event/{name}/video.mp4"
        meta = self._load_event_meta(event_dir)
        if meta:
            event.update(meta)
        # Resolve the category for the UI filter: prefer the recognized detection,
        # then an accepted class label (covers events classified before detected_label
        # existed), then the folder suffix, otherwise treat it as plain motion.
        classification = event.get("classification") or {}
        class_label = classification.get("class_label")
        event["category"] = (
            classification.get("detected_label")
            or (class_label if class_label in self.EVENT_CATEGORIES else None)
            or event.get("category")
            or "movimento"
        )
        if include_frames:
            event["frames"] = [
                f"/motion_event/{event_dir.name}/{frame_path.name}" for frame_path in frames
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
            event["frames"] = [f"/motion_capture/{path.name}" for path, _ in grouped_paths]
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

    def _meta_path(self, event_dir: Path) -> Path:
        return event_dir / self.META_FILE_NAME

    def _load_event_meta(self, event_dir: Path) -> dict:
        meta_path = self._meta_path(event_dir)
        if not meta_path.exists():
            return {}
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
        return {}

    def _close_event_dir(self, event_dir: Path) -> None:
        if not event_dir.exists():
            return
        self._marker_path(event_dir).touch(exist_ok=True)

    def _ensure_event_is_closed(self, event_dir: Path) -> bool:
        marker_path = self._marker_path(event_dir)
        if marker_path.exists():
            return True
        is_current_event = (
            self.current_event_dir is not None and event_dir == self.current_event_dir
        )
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
