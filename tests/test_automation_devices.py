import shutil
import tempfile
import unittest
from pathlib import Path

from blackframe.automation.devices import (
    DeviceError,
    MockDevice,
    SmartDevice,
    TuyaLanDevice,
    build_device,
)
from blackframe.automation.events import EventContext
from blackframe.automation.registry import DeviceRegistry


class EventContextTests(unittest.TestCase):
    def test_category_maps_to_event_name(self):
        self.assertEqual(EventContext("e", "persona").event_name, "person_detected")
        self.assertEqual(EventContext("e", "animale_domestico").event_name, "animal_detected")
        self.assertEqual(EventContext("e", "movimento").event_name, "motion_detected")

    def test_unknown_category_falls_back_to_motion(self):
        self.assertEqual(EventContext("e", "boh").event_name, "motion_detected")

    def test_is_frozen(self):
        ctx = EventContext("e", "persona", source="ingresso")
        with self.assertRaises(Exception):
            ctx.event_id = "x"  # type: ignore[misc]


class MockDeviceTests(unittest.TestCase):
    def test_records_calls_and_tracks_state(self):
        device = MockDevice("luce")
        device.turn_on()
        device.set_state({"brightness": 50})
        device.turn_off()
        self.assertEqual(
            device.calls,
            [("turn_on", None), ("set_state", {"brightness": 50}), ("turn_off", None)],
        )
        self.assertIs(device.is_on, False)
        self.assertEqual(device.last_state, {"brightness": 50})

    def test_satisfies_protocol(self):
        self.assertIsInstance(MockDevice("x"), SmartDevice)

    def test_fail_raises_device_error(self):
        device = MockDevice("rotta", fail=True)
        with self.assertRaises(DeviceError):
            device.turn_on()


class BuildDeviceTests(unittest.TestCase):
    def test_builds_tuya_lan(self):
        device = build_device(
            {
                "name": "luce",
                "driver": "tuya_lan",
                "device_id": "abc",
                "ip": "192.168.1.10",
                "local_key": "secret",
            }
        )
        self.assertIsInstance(device, TuyaLanDevice)
        self.assertEqual(device.name, "luce")

    def test_default_driver_is_tuya_lan(self):
        device = build_device(
            {"name": "luce", "device_id": "abc", "ip": "192.168.1.10", "local_key": "k"}
        )
        self.assertIsInstance(device, TuyaLanDevice)

    def test_builds_mock(self):
        self.assertIsInstance(build_device({"name": "m", "driver": "mock"}), MockDevice)

    def test_unknown_driver_raises(self):
        with self.assertRaises(DeviceError):
            build_device({"name": "x", "driver": "zigbee"})

    def test_missing_name_raises(self):
        with self.assertRaises(DeviceError):
            build_device({"driver": "mock"})

    def test_tuya_lan_incomplete_raises(self):
        with self.assertRaises(DeviceError):
            build_device({"name": "luce", "driver": "tuya_lan", "device_id": "abc"})


class _FakeTuyaClient:
    """Client tinytuya finto: registra il DP passato a turn_on/turn_off."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def turn_on(self, switch=1, nowait=False):
        self.calls.append(("turn_on", switch))

    def turn_off(self, switch=1, nowait=False):
        self.calls.append(("turn_off", switch))


class TuyaLanDeviceLazyImportTests(unittest.TestCase):
    """Il driver non deve importare tinytuya finché non lo si usa davvero."""

    def test_construct_without_tinytuya_installed(self):
        # La costruzione non tocca tinytuya: nessun errore anche senza la dep.
        device = TuyaLanDevice("luce", "abc", "192.168.1.10", "key")
        self.assertEqual(device.name, "luce")


class TuyaLanDeviceSwitchDpTests(unittest.TestCase):
    """``switch_dp`` instrada on/off sul datapoint giusto (1 prese, 20 lampade)."""

    def _device_with_fake_client(self, switch_dp):
        device = TuyaLanDevice("d", "abc", "192.168.1.10", "key", switch_dp=switch_dp)
        fake = _FakeTuyaClient()
        device._client = fake  # bypassa _get_client (niente tinytuya)
        return device, fake

    def test_default_switch_dp_is_one(self):
        device, fake = self._device_with_fake_client(switch_dp=1)
        device.turn_on()
        device.turn_off()
        self.assertEqual(fake.calls, [("turn_on", 1), ("turn_off", 1)])

    def test_lamp_uses_dp_twenty(self):
        device, fake = self._device_with_fake_client(switch_dp=20)
        device.turn_on()
        device.turn_off()
        self.assertEqual(fake.calls, [("turn_on", 20), ("turn_off", 20)])

    def test_build_device_passes_switch_dp(self):
        device = build_device(
            {
                "name": "lampada",
                "driver": "tuya_lan",
                "device_id": "abc",
                "ip": "192.168.1.10",
                "local_key": "k",
                "switch_dp": 20,
            }
        )
        self.assertEqual(device._switch_dp, 20)


class DeviceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tuya-registry-"))
        self.store = self.tmpdir / "tuya_devices.json"
        self.registry = DeviceRegistry(
            store_path=self.store,
            key_path=self.tmpdir / ".tuya_devices.key",
            device_factory=lambda cfg: MockDevice(cfg["name"]),
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save_lamp(self, name="luce_ingresso", local_key="local-secret"):
        return self.registry.save_device(
            {
                "name": name,
                "driver": "tuya_lan",
                "device_id": "dev123",
                "ip": "192.168.1.10",
                "local_key": local_key,
                "version": 3.4,
            }
        )

    def test_save_and_roundtrip_config(self):
        self._save_lamp()
        config = self.registry.get_config("luce_ingresso")
        self.assertEqual(config["device_id"], "dev123")
        self.assertEqual(config["local_key"], "local-secret")
        self.assertEqual(config["version"], 3.4)

    def test_switch_dp_roundtrips(self):
        self.registry.save_device(
            {
                "name": "lampada",
                "driver": "tuya_lan",
                "device_id": "dev123",
                "ip": "192.168.1.10",
                "local_key": "k",
                "switch_dp": 20,
            }
        )
        self.assertEqual(self.registry.get_config("lampada")["switch_dp"], 20)

    def test_switch_dp_defaults_to_one(self):
        self._save_lamp()
        self.assertEqual(self.registry.get_config("luce_ingresso")["switch_dp"], 1)

    def test_secret_encrypted_at_rest(self):
        self._save_lamp(local_key="top-secret")
        raw = self.store.read_text()
        self.assertNotIn("top-secret", raw)
        self.assertIn("enc::", raw)

    def test_store_file_is_private(self):
        self._save_lamp()
        mode = self.store.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_list_devices_redacts_secret(self):
        self._save_lamp(local_key="hide-me")
        listed = self.registry.list_devices()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["local_key"], "***")

    def test_get_builds_and_caches_device(self):
        self._save_lamp()
        device = self.registry.get("luce_ingresso")
        self.assertIsInstance(device, MockDevice)
        self.assertIs(self.registry.get("luce_ingresso"), device)

    def test_get_unknown_raises(self):
        with self.assertRaises(DeviceError):
            self.registry.get("inesistente")

    def test_upsert_preserves_secret_when_blank(self):
        self._save_lamp(local_key="keep-this")
        self.registry.save_device(
            {
                "name": "luce_ingresso",
                "driver": "tuya_lan",
                "device_id": "dev123",
                "ip": "192.168.1.99",  # cambio IP, local_key vuota
                "local_key": "",
            }
        )
        config = self.registry.get_config("luce_ingresso")
        self.assertEqual(config["ip"], "192.168.1.99")
        self.assertEqual(config["local_key"], "keep-this")

    def test_save_invalidates_cache(self):
        self._save_lamp()
        first = self.registry.get("luce_ingresso")
        self._save_lamp(local_key="rotated")
        self.assertIsNot(self.registry.get("luce_ingresso"), first)

    def test_delete_device(self):
        self._save_lamp()
        self.assertTrue(self.registry.delete_device("luce_ingresso"))
        self.assertEqual(self.registry.device_names(), [])
        self.assertFalse(self.registry.delete_device("luce_ingresso"))

    def test_missing_store_returns_empty(self):
        self.assertEqual(self.registry.device_names(), [])
        self.assertEqual(self.registry.list_devices(), [])

    def test_normalize_requires_name(self):
        with self.assertRaises(DeviceError):
            self.registry.save_device({"driver": "tuya_lan", "device_id": "x", "ip": "y"})

    def test_normalize_tuya_requires_device_id_and_ip(self):
        with self.assertRaises(DeviceError):
            self.registry.save_device({"name": "x", "driver": "tuya_lan"})

    def test_plaintext_secret_migrated_on_read(self):
        # Simula uno store legacy con local_key in chiaro: alla lettura viene
        # ri-cifrato su disco (migrazione), come fa CameraProfileService.
        import json

        self.store.write_text(
            json.dumps(
                [
                    {
                        "name": "legacy",
                        "driver": "tuya_lan",
                        "device_id": "d",
                        "ip": "192.168.1.5",
                        "local_key": "plain-key",
                    }
                ]
            )
        )
        config = self.registry.get_config("legacy")
        self.assertEqual(config["local_key"], "plain-key")
        raw = self.store.read_text()
        self.assertNotIn("plain-key", raw)
        self.assertIn("enc::", raw)


if __name__ == "__main__":
    unittest.main()
