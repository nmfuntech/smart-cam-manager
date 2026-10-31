"""Background deletion of old motion events to bound disk usage."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from blackframe.motion_detector import MotionDetector

logger = logging.getLogger(__name__)


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
        """Apply age and one global byte quota, including orphan camera folders."""
        interval = 3600.0
        motions = list(self._provider())
        roots: dict[Path, dict] = {}
        for motion in motions:
            interval = min(
                interval,
                float(motion.config.get("retention_interval_sec", 3600) or 3600),
            )
            save_dir = Path(motion.config.get("save_dir", "captures/motion"))
            # Per-camera layout is captures/motion/<profile-id>. Legacy installs may
            # point directly at captures/motion, so detect that shape explicitly.
            root = save_dir.parent if save_dir.name != "motion" else save_dir
            entry = roots.setdefault(
                root,
                {
                    "days": float(motion.config.get("retention_days", 0) or 0),
                    "max_mb": float(motion.config.get("retention_max_mb", 0) or 0),
                    "active": set(),
                },
            )
            current = getattr(motion._get_event_store(), "current_event_dir", None)
            if current is not None:
                entry["active"].add(Path(current).resolve())

        for root, config in roots.items():
            try:
                removed = self._purge_root(root, **config)
                if removed:
                    logger.info("Retention globale (%s): rimossi %s eventi", root, removed)
            except Exception:
                logger.exception("Errore retention globale (%s)", root)
        return interval

    @staticmethod
    def _purge_root(root: Path, days: float, max_mb: float, active: set[Path]) -> int:
        if not root.exists() or (days <= 0 and max_mb <= 0):
            return 0
        candidates: list[dict] = []
        protected_size = 0
        paths = [*root.glob("motion_event_*"), *root.glob("*/motion_event_*")]
        for path in paths:
            if not path.is_dir():
                continue
            try:
                size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
                if path.resolve() in active:
                    protected_size += size
                    continue
                candidates.append({"path": path, "time": path.stat().st_mtime, "size": size})
            except OSError:
                continue
        candidates.sort(key=lambda item: item["time"])
        removed = 0
        if days > 0:
            cutoff = time.time() - days * 86400
            survivors = []
            for item in candidates:
                if item["time"] < cutoff:
                    shutil.rmtree(item["path"], ignore_errors=True)
                    removed += 1
                else:
                    survivors.append(item)
            candidates = survivors
        if max_mb > 0:
            quota = max_mb * 1024 * 1024
            total = protected_size + sum(item["size"] for item in candidates)
            for item in candidates:
                if total <= quota:
                    break
                shutil.rmtree(item["path"], ignore_errors=True)
                total -= item["size"]
                removed += 1
        return removed

    def _run(self) -> None:
        # Run once shortly after boot, then on the configured interval.
        time.sleep(10)
        while True:
            interval = self._sweep()
            time.sleep(max(interval, 60))


# Chiavi env runtime -> chiavi lowercase della config del ContinuousRecorder.
