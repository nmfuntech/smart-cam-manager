"""PTZ control over ONVIF, with the SOAP/XML parser locked down (XXE/SSRF)."""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


_onvif_xml_hardened = False


def _harden_onvif_xml_parser() -> None:
    """Lock down the zeep/lxml settings onvif-zeep uses to parse camera responses.

    The camera talks ONVIF/SOAP in cleartext on the LAN, so its XML responses are
    untrusted input. onvif-zeep builds its zeep client with forbid_external unset
    and xml_huge_tree=True, leaving the door open to XXE (local file disclosure,
    e.g. reading .env), SSRF via remote schema imports, and entity-expansion DoS.
    We patch the Settings it constructs to forbid DTDs/entities/external refs and
    to keep libxml2's expansion limits. Idempotent; safe if onvif is absent.
    """
    global _onvif_xml_hardened
    if _onvif_xml_hardened:
        return
    try:
        import onvif.client as onvif_client
        from zeep import Settings
    except Exception:
        return

    class _HardenedSettings(Settings):
        # onvif-zeep sets `settings.xml_huge_tree = True` *after* construction;
        # expose it as a read-only False so that assignment is a no-op.
        @property
        def xml_huge_tree(self):
            return False

        @xml_huge_tree.setter
        def xml_huge_tree(self, value):
            pass

    def _hardened_settings_factory(*args, **kwargs):
        kwargs.setdefault("forbid_external", True)
        kwargs.setdefault("forbid_dtd", True)
        kwargs.setdefault("forbid_entities", True)
        return _HardenedSettings(*args, **kwargs)

    onvif_client.Settings = _hardened_settings_factory
    _onvif_xml_hardened = True


def _build_onvif_transport():
    """A zeep transport that refuses to fetch remote WSDL/XSD documents (SSRF guard).

    ONVIF WSDLs ship locally; the only remote references are http:// schema imports
    (e.g. w3.org/xmlsoap). Blocking remote *document loads* stops a MITM or a
    malicious schema from being pulled into the parser, while leaving the SOAP
    calls to the camera (transport.post) working normally.
    """
    from zeep.transports import Transport

    class _NoRemoteLoadTransport(Transport):
        def load(self, url):
            if str(url).lower().startswith(("http://", "https://")):
                raise RuntimeError(
                    f"Caricamento documento ONVIF remoto bloccato (anti-SSRF): {url}"
                )
            return super().load(url)

    return _NoRemoteLoadTransport()


class PTZController:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.camera = None
        self.media_service = None
        self.ptz_service = None
        self.profile_token = None
        self.continuous_request = None
        self.stop_request = None
        self.last_error = "PTZ non inizializzato"
        self.available = False

    def _setup(self) -> None:
        if not self.config["username"] or not self.config["password"]:
            self.last_error = "Credenziali ONVIF mancanti nel file .env"
            self.available = False
            return

        try:
            from onvif import ONVIFCamera

            # The camera is untrusted input over a cleartext LAN protocol: harden
            # the SOAP/XML parser (XXE/SSRF/DoS) and block remote document fetches.
            _harden_onvif_xml_parser()
            camera = ONVIFCamera(
                self.config["host"],
                self.config["port"],
                self.config["username"],
                self.config["password"],
                transport=_build_onvif_transport(),
            )
            media_service = camera.create_media_service()
            ptz_service = camera.create_ptz_service()
            profile = media_service.GetProfiles()[0]

            continuous_request = ptz_service.create_type("ContinuousMove")
            continuous_request.ProfileToken = profile.token

            stop_request = ptz_service.create_type("Stop")
            stop_request.ProfileToken = profile.token
            stop_request.PanTilt = True
            stop_request.Zoom = True

            self.camera = camera
            self.media_service = media_service
            self.ptz_service = ptz_service
            self.profile_token = profile.token
            self.continuous_request = continuous_request
            self.stop_request = stop_request
            self.last_error = ""
            self.available = True
        except ModuleNotFoundError:
            self.last_error = "Libreria ONVIF non installata. Installa onvif-zeep."
            self.available = False
        except Exception as exc:
            # Typical when the camera is offline: the cause fits in one line.
            reason = getattr(exc, "reason", None) or exc
            logger.warning("Connessione ONVIF fallita: %s", str(reason)[:200])
            self.last_error = "Connessione ONVIF fallita"
            self.available = False

    def move(self, direction: str) -> tuple[bool, str]:
        with self.lock:
            if not self.available:
                self._setup()

            if not self.available:
                return False, self.last_error

            vector_map = {
                "left": (-1.0, 0.0),
                "right": (1.0, 0.0),
                "up": (0.0, 1.0),
                "down": (0.0, -1.0),
                "up_left": (-0.7, 0.7),
                "up_right": (0.7, 0.7),
                "down_left": (-0.7, -0.7),
                "down_right": (0.7, -0.7),
            }

            if direction not in vector_map:
                return False, "Direzione PTZ non supportata"

            try:
                x, y = vector_map[direction]
                speed = self.config["move_speed"]
                self.continuous_request.Velocity = {
                    "PanTilt": {
                        "x": x * speed,
                        "y": y * speed,
                    }
                }
                self.ptz_service.ContinuousMove(self.continuous_request)
                time.sleep(self.config["move_timeout"])
                self.ptz_service.Stop(self.stop_request)
                self.last_error = ""
                return True, ""
            except Exception:
                logger.exception("Comando PTZ fallito")
                self.available = False
                self.last_error = "Comando PTZ fallito"
                return False, self.last_error

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if not self.available:
                self._setup()

            if not self.available:
                return False, self.last_error

            try:
                self.ptz_service.Stop(self.stop_request)
                self.last_error = ""
                return True, ""
            except Exception:
                logger.exception("Stop PTZ fallito")
                self.available = False
                self.last_error = "Stop PTZ fallito"
                return False, self.last_error

    def home(self) -> tuple[bool, str]:
        with self.lock:
            if not self.available:
                self._setup()

            if not self.available:
                return False, self.last_error

            try:
                request_home = self.ptz_service.create_type("GotoHomePosition")
                request_home.ProfileToken = self.profile_token
                self.ptz_service.GotoHomePosition(request_home)
                self.last_error = ""
                return True, ""
            except Exception:
                logger.exception("Home PTZ fallito")
                self.available = False
                self.last_error = "Home PTZ fallito"
                return False, self.last_error

    def get_status(self) -> dict:
        return {
            "available": self.available,
            "error": self.last_error,
            "host": self.config["host"],
            "port": self.config["port"],
        }

    def apply_runtime_config(self, updates: dict) -> None:
        with self.lock:
            reconnect = False
            if "TAPO_HOST" in updates:
                self.config["host"] = str(updates["TAPO_HOST"])
                reconnect = True
            if "TAPO_ONVIF_PORT" in updates:
                self.config["port"] = int(updates["TAPO_ONVIF_PORT"])
                reconnect = True
            if "TAPO_ONVIF_USERNAME" in updates:
                self.config["username"] = str(updates["TAPO_ONVIF_USERNAME"])
                reconnect = True
            if "TAPO_ONVIF_PASSWORD" in updates:
                self.config["password"] = str(updates["TAPO_ONVIF_PASSWORD"])
                reconnect = True
            if "TAPO_MOVE_SPEED" in updates:
                self.config["move_speed"] = float(updates["TAPO_MOVE_SPEED"])
            if "TAPO_MOVE_TIMEOUT" in updates:
                self.config["move_timeout"] = float(updates["TAPO_MOVE_TIMEOUT"])
            if reconnect:
                self.available = False
                self._setup()

    def probe_async(self) -> None:
        """Eagerly check ONVIF availability at startup and log the outcome.

        Runs in a daemon thread so a slow/unreachable camera does not block boot.
        """

        def _probe() -> None:
            with self.lock:
                self._setup()
            if self.available:
                logger.info(
                    "PTZ ONVIF disponibile su %s:%s",
                    self.config["host"],
                    self.config["port"],
                )
            else:
                logger.warning("PTZ ONVIF non disponibile: %s", self.last_error)

        threading.Thread(target=_probe, daemon=True).start()
