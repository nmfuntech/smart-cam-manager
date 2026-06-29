"""Dispatcher azioni: coda + thread daemon che esegue le azioni device.

Ricalca il pattern di ``TelegramNotifier._delivery_worker``: un thread daemon
permanente drena una coda ``_pending`` con isolamento totale degli errori.
Il thread motion chiama ``submit()`` e torna immediatamente; ogni interazione LAN
avviene qui, mai sul thread di video-analisi.

Resilienza per design:
- Ogni azione è isolata in try/except: un device che non risponde non blocca le
  azioni successive né risale verso la pipeline video.
- Timeout breve per azione (DEFAULT_SOCKET_TIMEOUT in devices.py, 5 s): il worker
  non resterà mai bloccato più di qualche secondo per device.
- Auto-off (``for_seconds > 0``): un timer daemon separato richiama turn_off sul
  device dopo il tempo configurato. Se fallisce → solo un log, non un crash.
- Il registry viene interpellato a submit-time (non al load): i device costruiti
  sul momento sfruttano le versioni aggiornate del registry.
"""

import logging
import threading
import time
from dataclasses import dataclass

from .engine import PlannedAction

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.1  # secondi di sleep tra poll della coda vuota


@dataclass
class _DispatchJob:
    planned: PlannedAction


class ActionDispatcher:
    """Esegue le azioni device su un thread daemon dedicato.

    Args:
        registry: ``DeviceRegistry`` da cui risolvere i device per nome logico.
    """

    def __init__(self, registry) -> None:
        self._registry = registry
        self._lock = threading.Lock()
        self._pending: list[_DispatchJob] = []
        self._worker_running = False

    # --- API pubblica -------------------------------------------------------

    def submit(self, planned: PlannedAction) -> None:
        """Accoda un'azione pianificata dall'engine. Non blocca mai."""
        self._start_worker()
        with self._lock:
            self._pending.append(_DispatchJob(planned=planned))

    # --- worker interno -----------------------------------------------------

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker_running:
                return
            self._worker_running = True
        threading.Thread(
            target=self._worker_loop, daemon=True, name="automation-dispatcher"
        ).start()

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                job = self._pending.pop(0) if self._pending else None
            if job is None:
                time.sleep(_POLL_INTERVAL)
                continue
            self._execute(job)

    def _execute(self, job: _DispatchJob) -> None:
        action = job.planned.action
        rule_name = job.planned.rule_name
        device_name = action.device
        try:
            device = self._registry.get(device_name)
        except Exception:
            logger.error(
                "Dispatcher: device '%s' non trovato nel registry (regola '%s')",
                device_name,
                rule_name,
            )
            return

        try:
            if action.action == "turn_on":
                device.turn_on()
                logger.info("Automazione [%s]: %s → turn_on", rule_name, device_name)
            elif action.action == "turn_off":
                device.turn_off()
                logger.info("Automazione [%s]: %s → turn_off", rule_name, device_name)
            elif action.action == "set_state":
                device.set_state(action.state or {})
                logger.info(
                    "Automazione [%s]: %s → set_state %s", rule_name, device_name, action.state
                )
        except Exception:
            logger.exception(
                "Automazione [%s]: azione '%s' su '%s' fallita",
                rule_name,
                action.action,
                device_name,
            )
            return

        # Auto-off: spegni il device dopo for_seconds su un timer daemon.
        if action.for_seconds > 0:
            self._schedule_off(device_name, rule_name, action.for_seconds)

    def _schedule_off(self, device_name: str, rule_name: str, delay: float) -> None:
        def _off():
            time.sleep(delay)
            try:
                device = self._registry.get(device_name)
                device.turn_off()
                logger.info(
                    "Automazione [%s]: %s → auto-off dopo %.0fs",
                    rule_name,
                    device_name,
                    delay,
                )
            except Exception:
                logger.exception(
                    "Automazione [%s]: auto-off di '%s' fallito",
                    rule_name,
                    device_name,
                )

        threading.Thread(target=_off, daemon=True, name=f"auto-off-{device_name}").start()
