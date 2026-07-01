import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blackframe.automation import DeviceRegistry
from blackframe.commands import COMMAND_REGISTRY, CommandArgSpec, execute, validate_arg

# ── Fakes minimi (stesso spirito di tests/test_automation_routes.py) ────────


class FakeCamera:
    def __init__(self, frame=b"jpeg-bytes"):
        self._frame = frame

    def get_frame(self):
        return self._frame

    def get_status(self):
        return {"connected": True, "connection_state": "online"}


class FakePtz:
    def __init__(self):
        self.moved = []

    def move(self, direction):
        self.moved.append(direction)
        return True, ""

    def stop(self):
        return True, ""

    def home(self):
        return True, ""

    def get_status(self):
        return {"available": True}


class FakeMotion:
    def __init__(self):
        self.config = {"classification_enabled": False, "record_enabled": False}
        self.classifier = None

    def get_status(self):
        return {"enabled": True, "motion_detected": False, "last_motion_at": None}

    def list_events(self, limit=8, include_frames=False):
        return []


class FakeContinuous:
    def status(self):
        return {"active": False}


class FakeRuntimeConfig:
    def __init__(self):
        self.updates = []

    def update(self, updates, allow_sensitive=False, allow_internal=False):
        self.updates.append(dict(updates))
        return {}


class FakeFeatures:
    telegram = None


class FakeServices:
    def __init__(self):
        self.camera = FakeCamera()
        self.ptz = FakePtz()
        self.motion = FakeMotion()
        self.continuous = FakeContinuous()
        self.features = FakeFeatures()
        self.runtime_config = FakeRuntimeConfig()
        self.automation_registry = None
        self.automation_engine = None
        self.applied = []

    def apply_runtime_config_all(self, updates):
        self.applied.append(dict(updates))


# ── validate_arg ─────────────────────────────────────────────────────────────


class ValidateArgTests(unittest.TestCase):
    def test_none_kind_ignores_extra_text(self):
        self.assertIsNone(validate_arg(None, "qualunque cosa"))
        self.assertIsNone(validate_arg(CommandArgSpec(kind="none"), None))

    def test_enum_normalizes_case_and_rejects_unknown(self):
        spec = CommandArgSpec(kind="enum", enum=("bassa", "media", "alta"))
        self.assertEqual(validate_arg(spec, "MEDIA"), "media")
        with self.assertRaises(ValueError):
            validate_arg(spec, "altissima")

    def test_name_rejects_invalid_characters(self):
        spec = CommandArgSpec(kind="name")
        self.assertEqual(validate_arg(spec, "luce_1"), "luce_1")
        with self.assertRaises(ValueError):
            validate_arg(spec, "Luce Salotto")

    def test_required_missing_raises(self):
        spec = CommandArgSpec(kind="name", required=True)
        with self.assertRaises(ValueError):
            validate_arg(spec, None)

    def test_optional_missing_returns_none(self):
        spec = CommandArgSpec(kind="float", required=False)
        self.assertIsNone(validate_arg(spec, ""))

    def test_int_and_float_reject_non_numeric(self):
        with self.assertRaises(ValueError):
            validate_arg(CommandArgSpec(kind="int"), "abc")
        with self.assertRaises(ValueError):
            validate_arg(CommandArgSpec(kind="float"), "abc")
        self.assertEqual(validate_arg(CommandArgSpec(kind="float"), "12.5"), "12.5")


# ── execute() / registro ──────────────────────────────────────────────────────


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.services = FakeServices()

    def test_unknown_command_raises(self):
        with self.assertRaises(ValueError):
            execute("does_not_exist", None, self.services)

    def test_clip_is_catalog_only_not_executable(self):
        self.assertIn("clip", COMMAND_REGISTRY)
        self.assertIsNone(COMMAND_REGISTRY["clip"].handler)
        with self.assertRaises(ValueError):
            execute("clip", "10", self.services)

    def test_status_returns_text(self):
        result = execute("status", None, self.services)
        self.assertIsNone(result.photo)
        self.assertIn("BLACKFRAME", result.text)

    def test_snapshot_no_frame(self):
        self.services.camera = FakeCamera(frame=None)
        result = execute("snapshot", None, self.services)
        self.assertIsNone(result.photo)
        self.assertEqual(result.text, "Nessun frame disponibile.")

    def test_snapshot_returns_photo(self):
        result = execute("snapshot", None, self.services)
        self.assertEqual(result.photo, b"jpeg-bytes")
        self.assertIsNone(result.text)

    def test_motion_toggle_applies_runtime_updates(self):
        result = execute("motion_off", None, self.services)
        self.assertEqual(self.services.runtime_config.updates, [{"MOTION_ENABLED": False}])
        self.assertEqual(self.services.applied, [{"MOTION_ENABLED": False}])
        self.assertIn("disattivato", result.text.lower())

    def test_sensitivity_valid_preset(self):
        result = execute("sensitivity", "alta", self.services)
        self.assertEqual(self.services.runtime_config.updates, [{"MOTION_THRESHOLD": 15}])
        self.assertIn("alta", result.text)

    def test_sensitivity_unknown_preset_does_not_update(self):
        result = execute("sensitivity", "altissima", self.services)
        self.assertEqual(self.services.runtime_config.updates, [])
        self.assertIn("sconosciuto", result.text.lower())

    def test_ptz_move(self):
        result = execute("ptz_left", None, self.services)
        self.assertEqual(self.services.ptz.moved, ["left"])
        self.assertIn("mosso", result.text.lower())

    def test_devices_without_registry(self):
        result = execute("devices", None, self.services)
        self.assertIn("non disponibile", result.text.lower())

    def test_rule_run_without_engine(self):
        result = execute("rule_run", "regola_test", self.services)
        self.assertIn("disabilitata", result.text.lower())


class DeviceRegistryBackedTests(unittest.TestCase):
    """Comandi device_on/device_off contro un DeviceRegistry reale (driver mock)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.services = FakeServices()
        self.services.automation_registry = DeviceRegistry(
            store_path=str(Path(self.tmp.name) / "devices.json")
        )
        self.services.automation_registry.save_device({"name": "luce_test", "driver": "mock"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_device_on_off_roundtrip(self):
        on_result = execute("device_on", "luce_test", self.services)
        self.assertIn("acceso", on_result.text)
        off_result = execute("device_off", "luce_test", self.services)
        self.assertIn("spento", off_result.text)

    def test_device_on_unknown_name_reports_error(self):
        result = execute("device_on", "non_esiste", self.services)
        self.assertIn("errore dispositivo", result.text.lower())

    def test_device_on_invalid_name_shows_usage(self):
        result = execute("device_on", "Nome Con Spazi", self.services)
        self.assertTrue(result.text.startswith("Uso: /device_on"))


class RuleRegistryBackedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rules_path = str(Path(self.tmp.name) / "rules.yaml")
        self.env_patch = mock.patch.dict(os.environ, {"AUTOMATION_RULES_PATH": self.rules_path})
        self.env_patch.start()
        self.services = FakeServices()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_rules_empty_by_default(self):
        result = execute("rules", None, self.services)
        self.assertIn("nessuna regola", result.text.lower())

    def test_rule_on_off_unknown_name(self):
        result = execute("rule_on", "sconosciuta", self.services)
        self.assertIn("non trovata", result.text.lower())


if __name__ == "__main__":
    unittest.main()
