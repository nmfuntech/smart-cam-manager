"""Bounded asynchronous dispatcher for smart-home actions."""

import heapq
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from .engine import PlannedAction

logger = logging.getLogger(__name__)


@dataclass
class _DispatchJob:
    planned: PlannedAction


class ActionDispatcher:
    """Execute device actions on one bounded, stoppable worker.

    Auto-off deadlines share the same worker. This avoids one sleeping thread per
    timer and lets a replaced automation engine shut down cleanly.
    """

    def __init__(self, registry, max_pending: int | None = None) -> None:
        self._registry = registry
        self._max_pending = max(
            1,
            max_pending
            if max_pending is not None
            else int(os.getenv("AUTOMATION_QUEUE_MAX", "64") or 64),
        )
        self._pending: deque[_DispatchJob] = deque()
        self._scheduled: list[tuple[float, int, str, str]] = []
        self._schedule_seq = 0
        self._condition = threading.Condition()
        self._worker_running = False
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, planned: PlannedAction) -> bool:
        """Queue action without blocking. Return False when bounded queue is full."""
        self._start_worker()
        with self._condition:
            if self._stopped:
                return False
            if len(self._pending) >= self._max_pending:
                logger.warning(
                    "Coda automazione piena (%d): azione %s/%s scartata",
                    self._max_pending,
                    planned.action.device,
                    planned.action.action,
                )
                return False
            self._pending.append(_DispatchJob(planned=planned))
            self._condition.notify()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._stopped = True
            self._pending.clear()
            self._scheduled.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def _start_worker(self) -> None:
        with self._condition:
            if self._worker_running or self._stopped:
                return
            self._worker_running = True
            self._thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="automation-dispatcher",
            )
            self._thread.start()

    def _worker_loop(self) -> None:
        try:
            while True:
                job = None
                scheduled = None
                with self._condition:
                    while not self._stopped:
                        now = time.monotonic()
                        if self._scheduled and self._scheduled[0][0] <= now:
                            scheduled = heapq.heappop(self._scheduled)
                            break
                        if self._pending:
                            job = self._pending.popleft()
                            break
                        timeout = (
                            max(0.0, self._scheduled[0][0] - now)
                            if self._scheduled
                            else None
                        )
                        self._condition.wait(timeout=timeout)
                    if self._stopped:
                        return
                if job is not None:
                    self._execute(job)
                elif scheduled is not None:
                    _deadline, _seq, device_name, rule_name = scheduled
                    self._execute_auto_off(device_name, rule_name)
        finally:
            with self._condition:
                self._worker_running = False

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
            elif action.action == "turn_off":
                device.turn_off()
            elif action.action == "set_state":
                device.set_state(action.state or {})
            logger.info("Automazione [%s]: %s → %s", rule_name, device_name, action.action)
        except Exception:
            logger.exception(
                "Automazione [%s]: azione '%s' su '%s' fallita",
                rule_name,
                action.action,
                device_name,
            )
            return

        if action.for_seconds > 0:
            self._schedule_off(device_name, rule_name, action.for_seconds)

    def _schedule_off(self, device_name: str, rule_name: str, delay: float) -> None:
        with self._condition:
            if self._stopped:
                return
            self._schedule_seq += 1
            heapq.heappush(
                self._scheduled,
                (time.monotonic() + max(0.0, delay), self._schedule_seq, device_name, rule_name),
            )
            self._condition.notify()

    def _execute_auto_off(self, device_name: str, rule_name: str) -> None:
        try:
            self._registry.get(device_name).turn_off()
            logger.info("Automazione [%s]: %s → auto-off", rule_name, device_name)
        except Exception:
            logger.exception("Automazione [%s]: auto-off di '%s' fallito", rule_name, device_name)
