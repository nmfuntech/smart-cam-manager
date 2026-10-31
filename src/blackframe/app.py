"""Composition root: wires the building blocks into the running application.

Camera, PTZ, motion, retention and settings live in their own modules; the
names marked ``noqa: F401`` are re-exported because callers and tests use
``blackframe.app.X``.
"""

import logging
import os
import threading
import time  # noqa: F401
from dataclasses import dataclass, field
from datetime import datetime  # noqa: F401
from pathlib import Path  # noqa: F401

import cv2  # noqa: F401
from flask import Flask

from blackframe.agent import AgentService, AgentTranscriptStore
from blackframe.auth import auth_bp, configure_auth
from blackframe.automation import (
    ActionDispatcher,
    AutomationEngine,
    DeviceRegistry,
    EventContext,  # noqa: F401
    load_rules,
)
from blackframe.camera_stream import CameraStream
from blackframe.classification import PersonPetClassifier  # noqa: F401
from blackframe.continuous_recording import ContinuousRecorder
from blackframe.env_config import (  # noqa: F401
    build_default_camera_profile,
    configure_opencv_runtime,
    get_motion_config,
    get_onvif_config,
    get_rtsp_url,
    get_stream_config,
    motion_config_for_profile,
    onvif_config_from_profile,
    profile_motion_dir,
    rtsp_url_from_profile,
)
from blackframe.hardening import (  # noqa: F401
    _PERMS_MARKER_NAME,
    harden_captures_permissions,
    harden_private_runtime_files,
)
from blackframe.health import HealthMonitor
from blackframe.modes import ModeManager, set_mode_manager
from blackframe.motion_detector import MotionDetector
from blackframe.motion_events import MotionEventStore  # noqa: F401
from blackframe.notifications import TelegramNotifier
from blackframe.ptz import (  # noqa: F401
    PTZController,
    _build_onvif_transport,
    _harden_onvif_xml_parser,
)
from blackframe.recording import EventRecorder
from blackframe.retention import RetentionJanitor
from blackframe.routes import register_blueprints
from blackframe.runtime_config import RuntimeConfigManager
from blackframe.service_layer import (
    CameraProfileService,
    FeatureServices,
    NotificationService,
    PresetService,
    RecordingService,
    WifiService,
)
from blackframe.telegram_commands import TelegramCommandBot
from scripts.runtime_paths import configure_runtime_environment

configure_runtime_environment()
logger = logging.getLogger(__name__)


_CONTINUOUS_KEY_MAP = {
    "CONTINUOUS_RECORD_ENABLED": "continuous_record_enabled",
    "CONTINUOUS_RECORD_SEGMENT_MIN": "continuous_record_segment_min",
    "CONTINUOUS_RECORD_RETAIN_HOURS": "continuous_record_retain_hours",
}


def _coerce_continuous_value(env_key: str, value):
    """Gli updates del PATCH arrivano raw dal client (bool/num/stringa): la
    config del recorder vuole tipi nativi."""
    if env_key == "CONTINUOUS_RECORD_ENABLED":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


@dataclass
class CameraRuntime:
    """Bundle of background workers for one monitored (non-active) camera profile."""

    profile_id: str
    camera: CameraStream
    motion: MotionDetector
    recorder: EventRecorder
    continuous: ContinuousRecorder | None = None
    ptz: PTZController | None = None

    def stop(self) -> None:
        self.motion.stop()
        self.camera.stop()
        if self.recorder is not None:
            self.recorder.stop()
        if self.continuous is not None:
            self.continuous.stop()


def build_camera_runtime(profile: dict, notifier=None, automation=None) -> CameraRuntime:
    camera = CameraStream(rtsp_url_from_profile(profile), get_stream_config())
    motion_config = motion_config_for_profile(profile)
    recorder = EventRecorder(camera, motion_config)
    continuous = ContinuousRecorder(camera, motion_config, camera_id=profile["id"])
    motion = MotionDetector(
        camera,
        motion_config,
        notifier=notifier,
        recorder=recorder,
        automation=automation,
        camera_id=profile["id"],
    )
    continuous.start()
    return CameraRuntime(
        profile_id=profile["id"],
        camera=camera,
        motion=motion,
        recorder=recorder,
        continuous=continuous,
    )


@dataclass
class AppServices:
    camera: CameraStream
    ptz: PTZController
    motion: MotionDetector
    features: FeatureServices
    runtime_config: RuntimeConfigManager
    monitors: dict = field(default_factory=dict)
    continuous: ContinuousRecorder | None = None
    telegram_commands: TelegramCommandBot | None = None
    automation_engine: AutomationEngine | None = None
    automation_registry: DeviceRegistry | None = None
    agent: AgentService | None = None
    health: HealthMonitor | None = None
    # Transcript della chat web /agente: esiste anche ad agente spento, così
    # la history resta leggibile dalla UI.
    agent_transcript: AgentTranscriptStore = field(default_factory=AgentTranscriptStore)

    def camera_and_motion(self, profile_id: str):
        """Resolve the (camera, motion) pair for a profile: active main pair or a monitor."""
        active_id = self.features.camera_profiles.get_active_profile_id()
        if profile_id == active_id:
            return self.camera, self.motion
        runtime = self.monitors.get(profile_id)
        if runtime is None:
            return None, None
        return runtime.camera, runtime.motion

    def start_monitor(self, profile_id: str) -> bool:
        active_id = self.features.camera_profiles.get_active_profile_id()
        if profile_id == active_id or profile_id in self.monitors:
            return True
        profile = self.features.camera_profiles.get_profile(profile_id)
        if not profile:
            return False
        self.monitors[profile_id] = build_camera_runtime(
            profile, self.features.telegram, self.automation_engine
        )
        return True

    def stop_monitor(self, profile_id: str) -> None:
        runtime = self.monitors.pop(profile_id, None)
        if runtime is not None:
            runtime.stop()

    def apply_runtime_config_all(self, updates: dict) -> None:
        """Apply a runtime config change to the active camera AND every monitor.

        Each monitored camera runs its own MotionDetector/classifier, so a setting
        like the person/pet filter must reach them too or they keep using the old
        config (e.g. still notifying for a category disabled from Telegram/UI)."""
        self.camera.apply_runtime_config(updates)
        self.ptz.apply_runtime_config(updates)
        self.motion.apply_runtime_config(updates)
        for runtime in self.monitors.values():
            runtime.camera.apply_runtime_config(updates)
            runtime.motion.apply_runtime_config(updates)
            # monitored cameras have no PTZ controller
        self._apply_continuous_config(updates)

    def _apply_continuous_config(self, updates: dict) -> None:
        """Propaga i cambi CONTINUOUS_RECORD_* ai recorder in esecuzione.

        Senza questo, il toggle da UI scrive l'env ma il ContinuousRecorder
        attivo mantiene la config vecchia fino al riavvio (apply_config avvia/
        ferma il segment loop da solo)."""
        if not any(key in _CONTINUOUS_KEY_MAP for key in updates):
            return
        if self.continuous is not None:
            # L'env è già aggiornato da RuntimeConfigManager.update: la config
            # della camera attiva si può rileggere per intero.
            self.continuous.apply_config(get_motion_config())
        for runtime in self.monitors.values():
            if runtime.continuous is None:
                continue
            # La config dei monitor deriva dal profilo (save_dir/camera propri):
            # merge delle sole chiavi continuous, mai get_motion_config().
            merged = dict(runtime.continuous.config)
            for env_key, cfg_key in _CONTINUOUS_KEY_MAP.items():
                if env_key in updates:
                    merged[cfg_key] = _coerce_continuous_value(env_key, updates[env_key])
            runtime.continuous.apply_config(merged)

    def reload_automation(self) -> None:
        """Ricostruisce DeviceRegistry + AutomationEngine e li ri-aggancia a tutti i MotionDetector.

        Thread-safe: _automation_reload_lock serializza ricostruzioni concorrenti.
        L'assegnazione su MotionDetector è atomica in CPython (GIL).
        Il vecchio ActionDispatcher daemon drains silenziosamente senza stop esplicito.
        """
        with _automation_reload_lock:
            old_engine = self.automation_engine
            try:
                registry = _build_registry()
                engine = _build_automation()
                self.automation_registry = registry
                self.automation_engine = engine
                self.motion.automation = engine
                for runtime in self.monitors.values():
                    runtime.motion.automation = engine
                if old_engine is not None and old_engine is not engine:
                    old_engine.stop()
                logger.info(
                    "Automazione ricaricata: %s · %d regole · %d device",
                    "attiva" if engine is not None else "disabilitata",
                    len(engine.rules) if engine is not None else 0,
                    len(registry.device_names()),
                )
            except Exception:
                logger.exception("Ricaricamento automazione fallito")

    def reload_agent(self) -> None:
        """Ricostruisce (o disattiva) il layer agentico dopo un toggle
        ``AGENT_ENABLED`` a runtime. Le eventuali proposte in attesa di
        conferma vengono perse — accettabile: l'utente può solo riproporle."""
        self.agent = _build_agent(self)
        logger.info("Agente ricaricato: %s", "attivo" if self.agent is not None else "disabilitato")


def _build_registry() -> DeviceRegistry:
    devices_path = os.getenv("AUTOMATION_DEVICES_PATH", "data/tuya_devices.json")
    return DeviceRegistry(store_path=devices_path)


_automation_reload_lock = threading.Lock()


def _build_automation() -> AutomationEngine | None:
    """Costruisce AutomationEngine + ActionDispatcher se AUTOMATION_ENABLED=true.

    Ritorna None se disabilitato o se il caricamento fallisce: il chiamante non
    deve mai crashare per un errore di configurazione dell'automazione.
    """
    if os.getenv("AUTOMATION_ENABLED", "false").lower() != "true":
        return None
    try:
        rules_path = os.getenv("AUTOMATION_RULES_PATH", "config/automation/rules.yaml")
        devices_path = os.getenv("AUTOMATION_DEVICES_PATH", "data/tuya_devices.json")
        registry = DeviceRegistry(store_path=devices_path)
        rules = load_rules(rules_path, known_devices=set(registry.device_names()))
        dispatcher = ActionDispatcher(registry)
        engine = AutomationEngine(rules, dispatcher=dispatcher)
        logger.info(
            "Automazione abilitata: %d regola/e, %d device",
            len(rules),
            len(registry.device_names()),
        )
        return engine
    except Exception:
        logger.exception("Avvio automazione fallito — layer disabilitato")
        return None


def _build_agent(services: "AppServices") -> AgentService | None:
    """Costruisce il layer agentico (NLU via Ollama) se AGENT_ENABLED=true.

    L'unica attivita' di rete alla costruzione e' il warm-up asincrono
    best-effort (thread daemon, mai bloccante) che precarica il modello in
    RAM: senza, il primo messaggio utente sul mini PC pagherebbe il
    caricamento da disco oltre AGENT_TIMEOUT_SEC. Se la variabile e'
    assente/false l'agente resta None e Telegram/Web rispondono "non
    abilitato" senza mai provare a contattare Ollama.
    """
    if os.getenv("AGENT_ENABLED", "false").lower() != "true":
        return None
    agent = AgentService(services)
    agent.start_warmup()
    return agent


def build_services() -> AppServices:
    harden_captures_permissions()
    harden_private_runtime_files()
    runtime_config = RuntimeConfigManager()
    camera_profiles = CameraProfileService()
    camera_profiles.ensure_default_profile(build_default_camera_profile())
    active_profile_id = camera_profiles.get_active_profile_id()
    if active_profile_id:
        active_profile = camera_profiles.get_profile(active_profile_id)
        if active_profile:
            runtime_config.update(
                camera_profiles.build_runtime_updates(active_profile),
                allow_sensitive=True,
                allow_internal=True,
            )
    motion_config = get_motion_config()
    camera = CameraStream(get_rtsp_url(), get_stream_config())
    ptz = PTZController(get_onvif_config())
    ptz.probe_async()
    notifier = TelegramNotifier()
    recorder = EventRecorder(camera, motion_config)
    continuous = ContinuousRecorder(camera, motion_config, camera_id=active_profile_id or "default")
    automation_registry = _build_registry()
    automation = _build_automation()
    motion = MotionDetector(
        camera,
        motion_config,
        notifier=notifier,
        recorder=recorder,
        automation=automation,
        camera_id=active_profile_id or "default",
    )
    continuous.start()
    features = FeatureServices(
        presets=PresetService(),
        notifications=NotificationService(),
        recording=RecordingService(),
        telegram=notifier,
        camera_profiles=camera_profiles,
        wifi=WifiService(),
    )

    # Start background runtimes for any additional profiles flagged as monitored,
    # so they capture events and fire notifications even while the viewer shows
    # the active camera.
    monitors: dict[str, CameraRuntime] = {}
    try:
        for summary in camera_profiles.list_profiles():
            pid = summary.get("id")
            if not summary.get("monitored") or pid == active_profile_id:
                continue
            full_profile = camera_profiles.get_profile(pid)
            if full_profile:
                monitors[pid] = build_camera_runtime(full_profile, notifier, automation)
    except Exception:
        logger.exception("Avvio monitor multi-camera fallito")

    services = AppServices(
        camera=camera,
        ptz=ptz,
        motion=motion,
        features=features,
        runtime_config=runtime_config,
        monitors=monitors,
        continuous=continuous,
        automation_engine=automation,
        automation_registry=automation_registry,
    )
    # Dopo AppServices così il provider vede anche i monitor avviati a runtime.
    RetentionJanitor(lambda: [services.motion, *(rt.motion for rt in services.monitors.values())])
    services.telegram_commands = TelegramCommandBot(services)
    services.telegram_commands.start()
    services.agent = _build_agent(services)
    services.health = HealthMonitor(services)
    _install_mode_manager(services)
    services.health.start()
    return services


def _install_mode_manager(services: AppServices) -> ModeManager:
    """Mode manager whose scheduled switches are announced on Telegram."""

    def on_change(previous: str, current: str, by: str) -> None:
        if by == "programmazione" and services.health is not None:
            services.health.send(f"🛡 Modalità cambiata da programmazione: {current}", silent=True)

    manager = ModeManager(on_change=on_change)
    set_mode_manager(manager)
    manager.start()
    return manager


def create_app(services: AppServices | None = None) -> Flask:
    app = Flask(__name__)
    configure_auth(app)
    app.config["services"] = services or build_services()
    app.register_blueprint(auth_bp)
    register_blueprints(app)
    return app


_app: Flask | None = None
_app_lock = threading.Lock()


def get_app() -> Flask:
    """The process-wide application, built on first use (thread-safe)."""
    global _app
    with _app_lock:
        if _app is None:
            _app = create_app()
        return _app


def __getattr__(name: str):
    # ``blackframe.app:app`` (gunicorn, waitress, the Windows service) keeps
    # working, but merely importing this module no longer opens the camera,
    # starts threads or reads credentials (PEP 562 lazy module attribute).
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    app = get_app()
    app.run(
        host=os.getenv("APP_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        debug=False,
        use_reloader=False,
        threaded=True,
    )
