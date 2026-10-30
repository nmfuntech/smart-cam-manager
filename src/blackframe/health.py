"""Health watchdog: tells the owner when the system itself is in trouble.

Motion alerts only fire when everything works. Before this module a camera
that dropped off the network, a stalled detection thread or a full disk were
silent: the owner found out days later. The monitor runs in its own thread and
sends Telegram messages for:

- camera offline for more than ``HEALTH_CAMERA_OFFLINE_MIN`` (and back online)
- motion detection stalled while the camera is online
- low free disk space on the captures drive
- application start (so unexpected restarts are visible)
- a daily, silent digest at ``DIGEST_TIME`` (events per category, false
  alarms, camera downtime, disk, mode, open warnings)

Alerts that cannot be delivered (internet down) are kept and retried. An
optional ``HEARTBEAT_URL`` (healthchecks.io style) is pinged periodically, so an
external service can alert when the whole mini PC is down - the one failure
no code running on the mini PC can report.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import urllib.request
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from blackframe import telegram_client
from blackframe.envutil import env_bool, env_float, env_str
from blackframe.modes import get_mode_manager
from blackframe.timewindows import parse_hhmm

logger = logging.getLogger(__name__)

_EVENT_DIR_RE = re.compile(r"^motion_event_(\d{8}_\d{6})(?:__([a-z_]+))?$")
CATEGORY_LABELS = {
    "persona": "🧍 persone",
    "animale_domestico": "🐕 animali",
    "movimento": "👣 movimento",
}


def _fmt_minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class _CameraWatch:
    __slots__ = ("offline_since", "alerted", "frames", "frames_changed_at", "stall_alerted")

    def __init__(self) -> None:
        self.offline_since: float | None = None
        self.alerted = False
        self.frames: int | None = None
        self.frames_changed_at: float = time.monotonic()
        self.stall_alerted = False


class HealthMonitor:
    def __init__(self, services: Any, interval_sec: float = 30.0) -> None:
        self.services = services
        self.interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watches: dict[str, _CameraWatch] = {}
        self._outbox: deque[tuple[str, bool]] = deque(maxlen=20)
        self._disk_alert_at: float | None = None
        self._last_heartbeat: float = 0.0
        self._state_path = Path(env_str("HEALTH_STATE_PATH", "data/health_state.json"))
        self._state = self._load_state()

    # --- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if env_bool("HEALTH_NOTIFY_STARTUP", True):
            self.notify_startup()
        # First check after a grace period: the stream needs a few seconds.
        if self._stop.wait(min(60.0, self.interval_sec * 2)):
            return
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("Controllo salute fallito")
            self._stop.wait(self.interval_sec)

    # --- persistence ------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        from blackframe.service_layer import _write_private_text

        try:
            _write_private_text(self._state_path, json.dumps(self._state))
        except Exception:
            logger.warning("Stato salute non salvato")

    # --- delivery ---------------------------------------------------------
    def _target(self) -> tuple[str, str] | None:
        if not env_bool("HEALTH_ALERTS_ENABLED", True):
            return None
        token = env_str("NOTIFY_TELEGRAM_BOT_TOKEN")
        chat_id = env_str("NOTIFY_TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return None
        return token, chat_id

    def send(self, text: str, silent: bool = False) -> bool:
        """Send now, or keep in the outbox when Telegram is unreachable."""
        target = self._target()
        if target is None:
            logger.info("Avviso salute (Telegram non configurato): %s", text)
            return False
        result = telegram_client.send_message(target[0], target[1], text, silent=silent)
        if result.get("ok"):
            return True
        logger.warning("Avviso salute non inviato, ritento: %s", result.get("description"))
        self._outbox.append((text, silent))
        return False

    def _flush_outbox(self) -> None:
        target = self._target()
        if target is None:
            return
        while self._outbox:
            text, silent = self._outbox[0]
            result = telegram_client.send_message(
                target[0], target[1], text + "\n(avviso ritardato)", silent=silent
            )
            if not result.get("ok"):
                return
            self._outbox.popleft()

    # --- cameras ----------------------------------------------------------
    def _cameras(self) -> list[tuple[str, str, Any, Any]]:
        """(id, display name, camera stream, motion detector) for every running camera."""
        services = self.services
        cameras: list[tuple[str, str, Any, Any]] = []
        profiles = getattr(getattr(services, "features", None), "camera_profiles", None)

        def name_for(profile_id: str, fallback: str) -> str:
            try:
                profile = profiles.get_profile(profile_id) if profiles else None
            except Exception:
                profile = None
            return (profile or {}).get("name") or fallback

        active_id = "active"
        if profiles is not None:
            try:
                active_id = profiles.get_active_profile_id() or "active"
            except Exception:
                pass
        cameras.append(
            (active_id, name_for(active_id, "Camera principale"), services.camera, services.motion)
        )
        for profile_id, runtime in (getattr(services, "monitors", None) or {}).items():
            cameras.append(
                (profile_id, name_for(profile_id, profile_id), runtime.camera, runtime.motion)
            )
        return cameras

    def check_once(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._flush_outbox()
        offline_threshold = env_float("HEALTH_CAMERA_OFFLINE_MIN", 3.0, minimum=0.5) * 60
        stall_threshold = env_float("HEALTH_MOTION_STALL_SEC", 180.0, minimum=30.0)
        any_offline_alerted = False

        for cam_id, name, camera, motion in self._cameras():
            with self._lock:
                watch = self._watches.setdefault(cam_id, _CameraWatch())
            try:
                status = camera.get_status()
            except Exception:
                status = {"connection_state": "offline", "error": "stato non leggibile"}
            online = status.get("connection_state") == "online"

            if not online:
                if watch.offline_since is None:
                    watch.offline_since = now
                down_for = now - watch.offline_since
                if not watch.alerted and down_for >= offline_threshold:
                    watch.alerted = True
                    error = status.get("error") or "nessun frame"
                    self.send(
                        f"📵 {name} offline da {_fmt_minutes(down_for)}\nUltimo errore: {error}"
                    )
                if watch.alerted:
                    any_offline_alerted = True
            else:
                if watch.offline_since is not None:
                    down_for = now - watch.offline_since
                    self._add_downtime(down_for)
                    if watch.alerted:
                        self.send(
                            f"✅ {name} di nuovo online (offline per {_fmt_minutes(down_for)})"
                        )
                watch.offline_since = None
                watch.alerted = False

            self._check_motion_stall(name, motion, watch, online, now, stall_threshold)

        self._check_disk()
        self._heartbeat(healthy=not any_offline_alerted)
        self._maybe_send_digest()

    def _check_motion_stall(self, name, motion, watch, online, now, threshold) -> None:
        config = getattr(motion, "config", {}) or {}
        detecting = bool(config.get("enabled")) and get_mode_manager().allows_detection()
        frames = getattr(motion, "processed_frames", None)
        if not isinstance(frames, int) or not detecting or not online:
            watch.frames = frames if isinstance(frames, int) else None
            watch.frames_changed_at = now
            watch.stall_alerted = False
            return
        if frames != watch.frames:
            if watch.stall_alerted:
                self.send(f"✅ Rilevamento di {name} ripartito")
            watch.frames = frames
            watch.frames_changed_at = now
            watch.stall_alerted = False
            return
        if not watch.stall_alerted and now - watch.frames_changed_at >= threshold:
            watch.stall_alerted = True
            self.send(
                f"⚠️ Rilevamento di {name} bloccato da {_fmt_minutes(now - watch.frames_changed_at)}"
                " con la camera online. Prova /motion_off e /motion_on, o riavvia il servizio."
            )

    def _captures_root(self) -> Path:
        config = getattr(self.services.motion, "config", {}) or {}
        save_dir = Path(config.get("save_dir") or "captures/motion")
        return save_dir if save_dir.exists() else Path(".")

    def _disk_free_gb(self) -> float | None:
        try:
            return shutil.disk_usage(self._captures_root()).free / (1024**3)
        except OSError:
            return None

    def _check_disk(self) -> None:
        free_gb = self._disk_free_gb()
        minimum = env_float("HEALTH_DISK_MIN_FREE_GB", 3.0, minimum=0.1)
        if free_gb is None or free_gb >= minimum:
            self._disk_alert_at = None
            return
        now = time.monotonic()
        if self._disk_alert_at is None or now - self._disk_alert_at >= 12 * 3600:
            self._disk_alert_at = now
            self.send(
                f"💾 Spazio disco quasi esaurito: {free_gb:.1f} GB liberi."
                " Riduci MOTION_RETENTION_* o libera spazio."
            )

    def _add_downtime(self, seconds: float) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        downtime = self._state.setdefault("downtime", {})
        downtime[today] = float(downtime.get(today, 0.0)) + seconds
        # Keep one week.
        for day in sorted(downtime)[:-7]:
            downtime.pop(day, None)
        self._save_state()

    # --- heartbeat ----------------------------------------------------------
    def _heartbeat(self, healthy: bool) -> None:
        url = env_str("HEARTBEAT_URL")
        if not url or not url.lower().startswith(("http://", "https://")):
            return
        interval = env_float("HEARTBEAT_INTERVAL_SEC", 300.0, minimum=60.0)
        now = time.monotonic()
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        target = url if healthy else url.rstrip("/") + "/fail"
        try:
            with urllib.request.urlopen(target, timeout=10) as resp:  # noqa: S310 (admin URL)
                resp.read(64)
        except Exception as exc:
            logger.warning("Heartbeat non inviato: %s", exc)

    # --- warnings shown in /status and in the digest -----------------------
    def active_warnings(self) -> list[str]:
        warnings: list[str] = []
        motion = self.services.motion
        config = getattr(motion, "config", {}) or {}
        if not config.get("enabled", True):
            warnings.append("Rilevamento movimento spento (/motion_on per riattivarlo)")
        classifier = getattr(motion, "classifier", None)
        if config.get("classification_enabled") and classifier is not None:
            if not getattr(classifier, "ready", True):
                warnings.append("Modello di riconoscimento assente: solo movimento generico")
        from blackframe.mediatools import ffmpeg_bin

        if ffmpeg_bin() is None:
            warnings.append("ffmpeg non trovato: clip non riproducibili nel browser")
        free_gb = self._disk_free_gb()
        if free_gb is not None and free_gb < env_float("HEALTH_DISK_MIN_FREE_GB", 3.0, minimum=0.1):
            warnings.append(f"Disco quasi pieno ({free_gb:.1f} GB liberi)")
        with self._lock:
            for watch_id, watch in self._watches.items():
                if watch.alerted:
                    warnings.append(f"Camera {watch_id[:8]} offline")
        return warnings

    # --- startup + digest ---------------------------------------------------
    def notify_startup(self) -> None:
        from blackframe.version import get_version

        mode = get_mode_manager().mode
        self.send(
            f"🟢 BLACKFRAME avviato · versione {get_version()[:7]} · modalità {mode}",
            silent=True,
        )

    def _events_last_24h(self) -> tuple[Counter, int]:
        cutoff = datetime.now() - timedelta(hours=24)
        counts: Counter = Counter()
        false_alarms = 0
        roots = {self._captures_root()}
        for _, _, _, motion in self._cameras():
            save_dir = (getattr(motion, "config", {}) or {}).get("save_dir")
            if save_dir:
                roots.add(Path(save_dir))
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.glob("motion_event_*"):
                match = _EVENT_DIR_RE.match(path.name)
                if not match:
                    continue
                try:
                    when = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                except ValueError:
                    continue
                if when < cutoff:
                    continue
                counts[match.group(2) or "movimento"] += 1
                try:
                    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
                    if (meta.get("feedback") or {}).get("false_alarm"):
                        false_alarms += 1
                except (OSError, ValueError, AttributeError):
                    pass
        return counts, false_alarms

    def build_digest(self) -> str:
        counts, false_alarms = self._events_last_24h()
        total = sum(counts.values())
        lines = ["🗓 Riepilogo ultime 24 ore", ""]
        if total:
            parts = [f"{CATEGORY_LABELS.get(k, k)} {v}" for k, v in counts.most_common()]
            lines.append(f"Eventi: {total} ({', '.join(parts)})")
        else:
            lines.append("Eventi: nessuno")
        if false_alarms:
            lines.append(f"Segnalati come falsi allarmi: {false_alarms}")
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        downtime = self._state.get("downtime", {})
        down = float(downtime.get(today, 0)) + float(downtime.get(yesterday, 0))
        lines.append(f"Camera offline: {_fmt_minutes(down) if down >= 60 else 'mai'}")
        free_gb = self._disk_free_gb()
        if free_gb is not None:
            lines.append(f"Disco libero: {free_gb:.1f} GB")
        lines.append(f"Modalità: {get_mode_manager().mode}")
        warnings = self.active_warnings()
        if warnings:
            lines.append("")
            lines.extend(f"⚠️ {item}" for item in warnings)
        return "\n".join(lines)

    def _maybe_send_digest(self, now: datetime | None = None) -> None:
        when = parse_hhmm(env_str("DIGEST_TIME", "21:00"))
        if when is None:
            return
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self._state.get("digest_sent") == today:
            return
        if (now.hour, now.minute) < (when.hour, when.minute):
            return
        # Do not send a stale digest hours late (e.g. after a long outage).
        scheduled = now.replace(hour=when.hour, minute=when.minute, second=0, microsecond=0)
        if now - scheduled > timedelta(hours=2):
            self._state["digest_sent"] = today
            self._save_state()
            return
        self._state["digest_sent"] = today
        self._save_state()
        self.send(self.build_digest(), silent=True)
