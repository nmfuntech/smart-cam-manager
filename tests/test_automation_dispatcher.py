"""Test del dispatcher: isolamento errori, auto-off, routing azioni."""

import time
import unittest

from blackframe.automation.devices import DeviceError, MockDevice
from blackframe.automation.dispatcher import ActionDispatcher
from blackframe.automation.engine import PlannedAction
from blackframe.automation.rules import Action


def _make_job(device: str, action: str, for_seconds: float = 0.0) -> PlannedAction:
    return PlannedAction(
        rule_name="test_rule",
        action=Action(device=device, action=action, for_seconds=for_seconds),
    )


class _MockRegistry:
    """Registry finto che serve MockDevice per nome."""

    def __init__(self, devices: dict[str, MockDevice]):
        self._devices = devices

    def get(self, name: str) -> MockDevice:
        if name not in self._devices:
            raise DeviceError(f"Device '{name}' non trovato")
        return self._devices[name]


def _wait(condition, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


class ActionDispatcherTests(unittest.TestCase):
    def _dispatcher(self, **devices):
        mocks = {name: MockDevice(name) for name in devices} if not devices else {}
        for name, dev in devices.items():
            if isinstance(dev, MockDevice):
                mocks[name] = dev
            else:
                mocks[name] = MockDevice(name)
        return ActionDispatcher(_MockRegistry(mocks)), mocks

    def test_turn_on_dispatched(self):
        lamp = MockDevice("lamp")
        d = ActionDispatcher(_MockRegistry({"lamp": lamp}))
        d.submit(_make_job("lamp", "turn_on"))
        assert _wait(lambda: lamp.calls), "turn_on non eseguito"
        self.assertEqual(lamp.calls[0], ("turn_on", None))
        self.assertTrue(lamp.is_on)

    def test_turn_off_dispatched(self):
        lamp = MockDevice("lamp")
        d = ActionDispatcher(_MockRegistry({"lamp": lamp}))
        d.submit(_make_job("lamp", "turn_off"))
        assert _wait(lambda: lamp.calls)
        self.assertEqual(lamp.calls[0], ("turn_off", None))
        self.assertFalse(lamp.is_on)

    def test_set_state_dispatched(self):
        lamp = MockDevice("lamp")
        d = ActionDispatcher(_MockRegistry({"lamp": lamp}))
        job = PlannedAction(
            rule_name="r",
            action=Action(device="lamp", action="set_state", state={"brightness": 80}),
        )
        d.submit(job)
        assert _wait(lambda: lamp.calls)
        self.assertEqual(lamp.calls[0], ("set_state", {"brightness": 80}))

    def test_device_error_does_not_propagate(self):
        broken = MockDevice("broken", fail=True)
        d = ActionDispatcher(_MockRegistry({"broken": broken}))
        d.submit(_make_job("broken", "turn_on"))
        assert _wait(lambda: broken.calls), "azione non tentata"
        # nessuna eccezione propagata; worker ancora attivo
        lamp = MockDevice("lamp")
        d._registry._devices["lamp"] = lamp
        d.submit(_make_job("lamp", "turn_on"))
        assert _wait(lambda: lamp.calls), "worker bloccato dopo DeviceError"

    def test_unknown_device_does_not_crash_worker(self):
        lamp = MockDevice("lamp")
        d = ActionDispatcher(_MockRegistry({"lamp": lamp}))
        d.submit(_make_job("inesistente", "turn_on"))
        d.submit(_make_job("lamp", "turn_on"))
        assert _wait(lambda: lamp.calls), "worker bloccato dopo device inesistente"

    def test_auto_off_fires_after_delay(self):
        lamp = MockDevice("lamp")
        d = ActionDispatcher(_MockRegistry({"lamp": lamp}))
        d.submit(_make_job("lamp", "turn_on", for_seconds=0.1))
        assert _wait(lambda: lamp.is_on is True), "turn_on non eseguito"
        assert _wait(lambda: lamp.is_on is False, timeout=1.0), "auto-off non eseguito"
        ops = [c[0] for c in lamp.calls]
        self.assertEqual(ops, ["turn_on", "turn_off"])

    def test_multiple_jobs_processed_in_order(self):
        lamp = MockDevice("lamp")
        d = ActionDispatcher(_MockRegistry({"lamp": lamp}))
        d.submit(_make_job("lamp", "turn_on"))
        d.submit(_make_job("lamp", "turn_off"))
        assert _wait(lambda: len(lamp.calls) >= 2, timeout=2.0)
        self.assertEqual([c[0] for c in lamp.calls[:2]], ["turn_on", "turn_off"])

    def test_worker_starts_lazily(self):
        d = ActionDispatcher(_MockRegistry({}))
        self.assertFalse(d._worker_running)
        lamp = MockDevice("lamp")
        d._registry._devices["lamp"] = lamp
        d.submit(_make_job("lamp", "turn_on"))
        self.assertTrue(d._worker_running)


if __name__ == "__main__":
    unittest.main()
