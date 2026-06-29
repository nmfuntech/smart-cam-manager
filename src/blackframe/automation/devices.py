"""Astrazione device + driver Tuya LAN.

``SmartDevice`` è il contratto comune (Protocol) su cui poggiano regole e
dispatcher: aggiungere un nuovo ecosistema in futuro significa scrivere una nuova
implementazione, senza toccare engine/regole. ``TuyaLanDevice`` è il driver
concreto via ``tinytuya`` (controllo locale, niente cloud). ``MockDevice`` permette
di testare l'intera automazione senza hardware reale.
"""

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Driver supportati (campo ``driver`` nello store device).
DRIVER_TUYA_LAN = "tuya_lan"
DRIVER_MOCK = "mock"

# Timeout di rete per le chiamate LAN ai device Tuya (secondi). Tenuto basso: un
# device che non risponde non deve trattenere il worker del dispatcher.
DEFAULT_SOCKET_TIMEOUT = 5.0


class DeviceError(RuntimeError):
    """Errore di controllo device: usato per isolare i fallimenti del driver.

    Il dispatcher cattura questa eccezione per regola/azione, così un device che
    non risponde non propaga mai verso il thread di video-analisi.
    """


@runtime_checkable
class SmartDevice(Protocol):
    """Contratto comune per ogni device attuabile."""

    name: str

    def turn_on(self) -> None: ...

    def turn_off(self) -> None: ...

    def set_state(self, state: dict) -> None: ...


class TuyaLanDevice:
    """Device Tuya controllato in rete locale via ``tinytuya``.

    ``tinytuya`` è importato pigramente: la suite di test gira con ``MockDevice``
    e non richiede la dipendenza installata. Il client tinytuya viene costruito al
    primo utilizzo e riusato.
    """

    def __init__(
        self,
        name: str,
        device_id: str,
        ip: str,
        local_key: str,
        version: float = 3.3,
        socket_timeout: float = DEFAULT_SOCKET_TIMEOUT,
        switch_dp: int = 1,
    ) -> None:
        if not device_id or not ip or not local_key:
            raise DeviceError(f"Device Tuya '{name}' incompleto: servono device_id, ip e local_key")
        self.name = name
        self._device_id = device_id
        self._ip = ip
        self._local_key = local_key
        self._version = float(version or 3.3)
        self._socket_timeout = float(socket_timeout)
        # DP (datapoint) dell'interruttore on/off. Le prese Tuya usano il DP 1
        # (default OutletDevice); le lampade RGBCW Alantop usano il DP 20
        # (``switch_led``). Configurabile per device così ``turn_on``/``turn_off``
        # funzionano per entrambi senza cambiare le regole.
        self._switch_dp = int(switch_dp or 1)
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import tinytuya  # noqa: PLC0415 — import pigro: dep opzionale a runtime
        except ImportError as exc:  # pragma: no cover - dipende dall'ambiente
            raise DeviceError(
                "tinytuya non installato: esegui 'poetry install' per il controllo Tuya"
            ) from exc
        client = tinytuya.OutletDevice(self._device_id, self._ip, self._local_key)
        client.set_version(self._version)
        client.set_socketTimeout(self._socket_timeout)
        self._client = client
        return client

    @staticmethod
    def _check_response(response, name: str, action: str) -> None:
        """tinytuya ritorna un dict con chiave ``Error`` sui fallimenti, non solleva."""
        if isinstance(response, dict) and response.get("Error"):
            raise DeviceError(f"Device Tuya '{name}': {action} fallita ({response.get('Error')})")

    def turn_on(self) -> None:
        response = self._get_client().turn_on(switch=self._switch_dp)
        self._check_response(response, self.name, "turn_on")

    def turn_off(self) -> None:
        response = self._get_client().turn_off(switch=self._switch_dp)
        self._check_response(response, self.name, "turn_off")

    def set_state(self, state: dict) -> None:
        if not isinstance(state, dict) or not state:
            raise DeviceError(f"Device Tuya '{self.name}': set_state richiede un dict non vuoto")
        client = self._get_client()
        response = client.set_multiple_values({str(k): v for k, v in state.items()})
        self._check_response(response, self.name, "set_state")


class MockDevice:
    """Device finto per i test: registra le chiamate, opzionalmente fallisce.

    Mantiene anche uno stato on/off così i test possono verificare l'idempotenza
    ("accendi se non già accesa") nelle fasi successive.
    """

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[tuple[str, dict | None]] = []
        self.is_on: bool | None = None
        self.last_state: dict | None = None

    def _maybe_fail(self, action: str) -> None:
        if self.fail:
            raise DeviceError(f"MockDevice '{self.name}': {action} fallita (fail=True)")

    def turn_on(self) -> None:
        self.calls.append(("turn_on", None))
        self._maybe_fail("turn_on")
        self.is_on = True

    def turn_off(self) -> None:
        self.calls.append(("turn_off", None))
        self._maybe_fail("turn_off")
        self.is_on = False

    def set_state(self, state: dict) -> None:
        self.calls.append(("set_state", dict(state)))
        self._maybe_fail("set_state")
        self.last_state = dict(state)


def build_device(config: dict) -> SmartDevice:
    """Costruisce un ``SmartDevice`` dalla config (segreti già decifrati).

    Il campo ``driver`` seleziona l'implementazione. Sollevare ``DeviceError`` per
    driver sconosciuti tiene il fallimento dentro al perimetro dell'automazione.
    """
    if not isinstance(config, dict):
        raise DeviceError("Config device non valida")
    name = str(config.get("name") or "").strip()
    if not name:
        raise DeviceError("Config device senza 'name'")
    driver = str(config.get("driver") or DRIVER_TUYA_LAN).strip()

    if driver == DRIVER_TUYA_LAN:
        return TuyaLanDevice(
            name=name,
            device_id=str(config.get("device_id") or ""),
            ip=str(config.get("ip") or ""),
            local_key=str(config.get("local_key") or ""),
            version=float(config.get("version") or 3.3),
            switch_dp=int(config.get("switch_dp") or 1),
        )
    if driver == DRIVER_MOCK:
        return MockDevice(name, fail=bool(config.get("fail", False)))
    raise DeviceError(f"Driver device sconosciuto: '{driver}'")
