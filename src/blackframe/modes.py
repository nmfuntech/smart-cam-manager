"""Security modes: armato / casa / disarmato, with an optional daily schedule.

The mode is a gate on top of the existing toggles, it never rewrites them:

- ``armato``    motion detection + recording + Telegram alerts (normal)
- ``casa``      detection and recording keep running, motion alerts are muted
                (people at home do not want a ping every time they walk by)
- ``disarmato`` detection paused entirely (privacy); health checks keep running

State is persisted in ``data/mode.json`` so a restart keeps the chosen mode.
``MODE_SCHEDULE`` (e.g. ``"08:30=armato,19:00=casa,23:30=armato"``) switches
automatically at those times; a manual change lasts until the next entry.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from blackframe.envutil import env_str
from blackframe.timewindows import parse_hhmm

logger = logging.getLogger(__name__)

ARMED = "armato"
HOME = "casa"
DISARMED = "disarmato"
MODES = (ARMED, HOME, DISARMED)

MODE_LABELS = {
    ARMED: "🛡 Armato — rilevamento e avvisi attivi",
    HOME: "🏠 Casa — rilevamento e registrazione attivi, avvisi silenziati",
    DISARMED: "💤 Disarmato — rilevamento in pausa",
}

_ALIASES = {
    "armato": ARMED,
    "arma": ARMED,
    "armed": ARMED,
    "away": ARMED,
    "fuori": ARMED,
    "casa": HOME,
    "home": HOME,
    "disarmato": DISARMED,
    "disarma": DISARMED,
    "disarmed": DISARMED,
    "off": DISARMED,
}


def normalize_mode(value: str | None) -> str | None:
    return _ALIASES.get((value or "").strip().lower())


def parse_schedule(raw: str) -> list[tuple[int, str]]:
    """Parse "HH:MM=mode,..." into sorted (minute_of_day, mode). Bad items are skipped."""
    entries: list[tuple[int, str]] = []
    for item in (raw or "").split(","):
        if "=" not in item:
            continue
        when_raw, mode_raw = item.split("=", 1)
        when = parse_hhmm(when_raw)
        mode = normalize_mode(mode_raw)
        if when is None or mode is None:
            logger.warning("MODE_SCHEDULE: voce ignorata %r", item.strip())
            continue
        entries.append((when.hour * 60 + when.minute, mode))
    return sorted(set(entries))


class ModeManager:
    def __init__(
        self,
        state_path: str | Path | None = None,
        on_change: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._path = Path(state_path or env_str("MODE_STATE_PATH", "data/mode.json"))
        self._lock = threading.Lock()
        self._mode = ARMED
        self._since = time.time()
        self._by = "default"
        self._on_change = on_change
        self._last_schedule_key: tuple[date, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- state ------------------------------------------------------------
    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def snapshot(self) -> dict:
        with self._lock:
            return {"mode": self._mode, "since": self._since, "by": self._by}

    def allows_detection(self) -> bool:
        return self.mode != DISARMED

    def allows_alerts(self) -> bool:
        return self.mode == ARMED

    def set_mode(self, mode: str, by: str = "manuale") -> bool:
        """Switch mode. Returns True when the mode actually changed."""
        normalized = normalize_mode(mode)
        if normalized is None:
            raise ValueError(f"Modalità sconosciuta: {mode}")
        with self._lock:
            previous = self._mode
            if previous == normalized:
                return False
            self._mode = normalized
            self._since = time.time()
            self._by = by
            self._save_locked()
        logger.info("Modalità: %s -> %s (%s)", previous, normalized, by)
        if self._on_change is not None:
            try:
                self._on_change(previous, normalized, by)
            except Exception:
                logger.exception("Callback cambio modalità fallita")
        return True

    def describe(self) -> str:
        snap = self.snapshot()
        since = datetime.fromtimestamp(snap["since"]).strftime("%d/%m %H:%M")
        text = f"{MODE_LABELS[snap['mode']]}\nDal {since} ({snap['by']})"
        schedule = parse_schedule(env_str("MODE_SCHEDULE"))
        if schedule:
            items = ", ".join(f"{m // 60:02d}:{m % 60:02d} {mode}" for m, mode in schedule)
            text += f"\nProgrammazione: {items}"
        return text

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        mode = normalize_mode(data.get("mode")) if isinstance(data, dict) else None
        if mode:
            self._mode = mode
            self._since = float(data.get("since") or time.time())
            self._by = str(data.get("by") or "salvato")

    def _save_locked(self) -> None:
        from blackframe.service_layer import _write_private_text

        try:
            _write_private_text(
                self._path,
                json.dumps({"mode": self._mode, "since": self._since, "by": self._by}),
            )
        except Exception:
            logger.warning("Stato modalità non salvato")

    # --- schedule -----------------------------------------------------------
    def apply_schedule(self, now: datetime | None = None) -> str | None:
        """Apply the schedule entry that matches the current minute (once per day).

        Returns the mode that was set, or None. Called every ~20 s by the
        background thread; matching on the exact minute keeps manual overrides
        in place until the next scheduled time.
        """
        schedule = parse_schedule(env_str("MODE_SCHEDULE"))
        if not schedule:
            return None
        now = now or datetime.now()
        minute = now.hour * 60 + now.minute
        for when, mode in schedule:
            if when != minute:
                continue
            key = (now.date(), when)
            if self._last_schedule_key == key:
                return None
            self._last_schedule_key = key
            self.set_mode(mode, by="programmazione")
            return mode
        return None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mode-schedule", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(20):
            try:
                self.apply_schedule()
            except Exception:
                logger.exception("Programmazione modalità fallita")


# Process-wide instance: the motion detectors and the notifier consult it
# without needing to be rebuilt when the mode changes.
_manager: ModeManager | None = None
_manager_lock = threading.Lock()


def get_mode_manager() -> ModeManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ModeManager()
        return _manager


def set_mode_manager(manager: ModeManager | None) -> None:
    """Install a specific manager (bootstrap/tests). ``None`` resets."""
    global _manager
    with _manager_lock:
        _manager = manager
