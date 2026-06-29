import json
import tempfile
import unittest
from pathlib import Path

import yaml

from blackframe.automation.tuya_import import (
    build_registry_payloads,
    infer_switch_dp,
    load_name_map,
    load_snapshot_by_id,
    load_tinytuya_devices,
    slugify_smart_name,
)


class SlugifyTests(unittest.TestCase):
    def test_normalizes_unicode_and_spaces(self):
        self.assertEqual(slugify_smart_name("Presa - Caffè"), "presa_caffe")

    def test_strips_noise(self):
        self.assertEqual(
            slugify_smart_name("1-Alantop Smart Desk Lamp - RGBCW"),
            "1_alantop_smart_desk_lamp_rgbcw",
        )


class InferSwitchDpTests(unittest.TestCase):
    def test_lamp_uses_twenty(self):
        self.assertEqual(infer_switch_dp({"20": True, "21": "colour"}), 20)

    def test_plug_uses_one(self):
        self.assertEqual(infer_switch_dp({"1": True, "20": 2352}), 1)

    def test_nested_dps_from_snapshot(self):
        devices = [
            {
                "name": "Lamp",
                "id": "lamp1",
                "key": "k" * 16,
                "ip": "192.168.1.10",
            }
        ]
        snapshot = {"lamp1": {"dps": {"dps": {"20": True}}}}
        payloads, _ = build_registry_payloads(devices, snapshot_by_id=snapshot)
        self.assertEqual(payloads[0]["switch_dp"], 20)

    def test_empty_defaults_to_one(self):
        self.assertEqual(infer_switch_dp(None), 1)


class BuildRegistryPayloadsTests(unittest.TestCase):
    def test_skips_offline_without_ip(self):
        devices = [
            {
                "name": "Presa - Dev",
                "id": "abc123",
                "key": "secretkey1234567",
                "ip": "",
            }
        ]
        payloads, skipped = build_registry_payloads(devices)
        self.assertEqual(payloads, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("IP mancante", skipped[0])

    def test_builds_online_device_with_map(self):
        devices = [
            {
                "name": "Presa - Caffè",
                "id": "dev1",
                "key": "localkey12345678",
                "ip": "192.168.1.19",
                "version": "3.3",
            }
        ]
        snapshot = {"dev1": {"dps": {"1": True}}}
        payloads, skipped = build_registry_payloads(
            devices,
            snapshot_by_id=snapshot,
            name_map={"Presa - Caffè": "presa_caffe"},
        )
        self.assertEqual(skipped, [])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name"], "presa_caffe")
        self.assertEqual(payloads[0]["switch_dp"], 1)
        self.assertEqual(payloads[0]["local_key"], "localkey12345678")


class LoadFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tuya-import-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_tinytuya_devices(self):
        path = self.tmpdir / "devices.json"
        path.write_text(
            json.dumps([{"name": "Lamp", "id": "x", "key": "k", "ip": "1.2.3.4"}]),
            encoding="utf-8",
        )
        devices = load_tinytuya_devices(path)
        self.assertEqual(len(devices), 1)

    def test_load_name_map_yaml(self):
        path = self.tmpdir / "map.yaml"
        path.write_text(yaml.dump({"Presa - Caffè": "presa_caffe"}), encoding="utf-8")
        self.assertEqual(load_name_map(path), {"Presa - Caffè": "presa_caffe"})

    def test_load_snapshot_by_id(self):
        path = self.tmpdir / "snapshot.json"
        path.write_text(
            json.dumps({"devices": [{"id": "abc", "dps": {"20": False}}]}),
            encoding="utf-8",
        )
        indexed = load_snapshot_by_id(path)
        self.assertIn("abc", indexed)
        self.assertEqual(indexed["abc"]["dps"]["20"], False)


if __name__ == "__main__":
    unittest.main()
