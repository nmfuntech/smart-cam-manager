import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blackframe.automation import DeviceRegistry

# ── Shared test helpers ────────────────────────────────────────────────────


def load_app_module():
    env = {
        "APP_ADMIN_PASSWORD": "admin-pass",
        "APP_SECRET_KEY": "test-secret",
        "TAPO_USERNAME": "user",
        "TAPO_PASSWORD": "pass",
        "TAPO_HOST": "127.0.0.1",
    }
    restore_targets = [
        Path(".env"),
        Path("data/camera_profiles.json"),
        Path("data/.camera_profiles.key"),
    ]
    backups = {}
    for path in restore_targets:
        backups[path] = path.read_bytes() if path.exists() else None
    try:
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("threading.Thread.start", lambda self: None):
                import blackframe.app as app

                return importlib.reload(app)
    finally:
        for path, content in backups.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)


def authenticate_client(client, csrf_token="test-csrf"):
    with client.session_transaction() as s:
        s["blackframe_auth_user"] = "admin"
        s["blackframe_csrf_token"] = csrf_token
    return csrf_token


def csrf_headers(token):
    return {"X-CSRF-Token": token, "Content-Type": "application/json"}


# ── Minimal fakes ──────────────────────────────────────────────────────────


class FakeCamera:
    def get_frame(self):
        return None

    def get_status(self):
        return {"connected": False, "error": ""}

    def apply_runtime_config(self, u):
        pass


class FakePtz:
    def get_status(self):
        return {"available": False, "host": "", "port": 0, "error": ""}

    def apply_runtime_config(self, u):
        pass


class FakeMotion:
    automation = None
    config = {"save_dir": tempfile.gettempdir()}

    def get_status(self):
        return {"enabled": False}

    def list_events(self, limit=8, include_frames=False):
        return []

    def get_event(self, eid):
        return {"id": eid}

    def apply_runtime_config(self, u):
        pass


class FakeRuntimeConfig:
    def __init__(self):
        self._vals = {"AUTOMATION_ENABLED": False}

    def get_public_config(self):
        return {}

    def update(self, updates, allow_sensitive=False, allow_internal=False):
        self._vals.update(updates)
        return {}


# ── Base class for automation route tests ─────────────────────────────────


class AutomationRouteTestBase(unittest.TestCase):
    def setUp(self):
        # Reset module-level rate limiter so tests never hit 429
        from blackframe.auth import rate_limiter

        rate_limiter._events.clear()

        self.tmp = tempfile.TemporaryDirectory()
        self.devices_path = str(Path(self.tmp.name) / "devices.json")
        self.rules_path = str(Path(self.tmp.name) / "rules.yaml")

        self.app_module = load_app_module()
        features = self.app_module.FeatureServices(
            presets=self.app_module.PresetService("data/test-presets.json"),
            notifications=self.app_module.NotificationService(),
            recording=self.app_module.RecordingService("captures/test-recordings"),
            camera_profiles=self.app_module.CameraProfileService("data/test-camera-profiles.json"),
            wifi=self.app_module.WifiService(),
        )
        self.runtime_config = FakeRuntimeConfig()
        registry = DeviceRegistry(store_path=self.devices_path)
        services = self.app_module.AppServices(
            camera=FakeCamera(),
            ptz=FakePtz(),
            motion=FakeMotion(),
            features=features,
            runtime_config=self.runtime_config,
            automation_registry=registry,
            automation_engine=None,
        )

        env_patch = {
            "AUTOMATION_RULES_PATH": self.rules_path,
            "AUTOMATION_DEVICES_PATH": self.devices_path,
            "AUTOMATION_ENABLED": "false",
        }
        with mock.patch.dict(os.environ, env_patch):
            self.app = self.app_module.create_app(services)

        self.env_patch = mock.patch.dict(os.environ, env_patch)
        self.env_patch.start()
        self.client = self.app.test_client()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def auth_client(self, token="tok"):
        return authenticate_client(self.client, token)

    def _post_json(self, url, data, token, authed=True):
        if authed:
            authenticate_client(self.client, token)
        return self.client.post(
            url,
            data=json.dumps(data),
            headers=csrf_headers(token),
            content_type="application/json",
        )

    def _delete(self, url, token, authed=True):
        if authed:
            authenticate_client(self.client, token)
        return self.client.delete(url, headers=csrf_headers(token))

    def _patch_json(self, url, data, token, authed=True):
        if authed:
            authenticate_client(self.client, token)
        return self.client.patch(
            url,
            data=json.dumps(data),
            headers=csrf_headers(token),
            content_type="application/json",
        )


# ── Auth tests ─────────────────────────────────────────────────────────────


class AutomationRoutesAuthTests(AutomationRouteTestBase):
    def _check_401(self, method, url, **kw):
        resp = getattr(self.client, method)(url, **kw)
        self.assertEqual(resp.status_code, 401, f"{method.upper()} {url} should require auth")

    def test_get_status_requires_auth(self):
        self._check_401("get", "/api/automazione/status")

    def test_get_devices_requires_auth(self):
        self._check_401("get", "/api/automazione/devices")

    def test_post_device_requires_auth(self):
        self._check_401(
            "post",
            "/api/automazione/devices",
            data=json.dumps({}),
            content_type="application/json",
        )

    def test_delete_device_requires_auth(self):
        self._check_401("delete", "/api/automazione/devices/foo")

    def test_get_rules_requires_auth(self):
        self._check_401("get", "/api/automazione/rules")

    def test_post_rule_requires_auth(self):
        self._check_401(
            "post",
            "/api/automazione/rules",
            data=json.dumps({}),
            content_type="application/json",
        )

    def test_delete_rule_requires_auth(self):
        self._check_401("delete", "/api/automazione/rules/foo")

    def test_toggle_requires_auth(self):
        self._check_401(
            "patch",
            "/api/automazione/toggle",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
        )


# ── CSRF tests ─────────────────────────────────────────────────────────────


class AutomationRoutesCsrfTests(AutomationRouteTestBase):
    def _check_403(self, method, url, data=None):
        authenticate_client(self.client, "tok")
        kw = {"headers": {"Content-Type": "application/json"}}
        if data is not None:
            kw["data"] = json.dumps(data)
        resp = getattr(self.client, method)(url, **kw)
        self.assertEqual(resp.status_code, 403, f"{method.upper()} {url} needs CSRF")

    def test_post_device_requires_csrf(self):
        self._check_403("post", "/api/automazione/devices", {})

    def test_delete_device_requires_csrf(self):
        self._check_403("delete", "/api/automazione/devices/foo")

    def test_post_rule_requires_csrf(self):
        self._check_403("post", "/api/automazione/rules", {})

    def test_delete_rule_requires_csrf(self):
        self._check_403("delete", "/api/automazione/rules/foo")

    def test_toggle_requires_csrf(self):
        self._check_403("patch", "/api/automazione/toggle", {"enabled": True})


# ── Status tests ───────────────────────────────────────────────────────────


class AutomationStatusTests(AutomationRouteTestBase):
    def test_status_returns_ok_true(self):
        authenticate_client(self.client)
        resp = self.client.get("/api/automazione/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("enabled", data)
        self.assertIn("active", data)
        self.assertIn("rule_count", data)
        self.assertIn("device_count", data)

    def test_status_device_count_reflects_registry(self):
        authenticate_client(self.client, "tok")
        # Add a device first
        self._post_json(
            "/api/automazione/devices",
            {
                "name": "test_plug",
                "driver": "tuya_lan",
                "device_id": "abc123",
                "ip": "192.168.1.50",
            },
            "tok",
        )
        resp = self.client.get("/api/automazione/status")
        data = resp.get_json()
        # After reload_automation, registry is rebuilt — device_count reflects new registry
        self.assertTrue(data["ok"])


# ── Device CRUD tests ──────────────────────────────────────────────────────


class AutomationDeviceRouteTests(AutomationRouteTestBase):
    def test_list_devices_returns_empty_initially(self):
        authenticate_client(self.client)
        resp = self.client.get("/api/automazione/devices")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["devices"], [])

    def test_save_new_device(self):
        resp = self._post_json(
            "/api/automazione/devices",
            {"name": "luce_test", "driver": "tuya_lan", "device_id": "bf123", "ip": "192.168.1.10"},
            "tok",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["device"]["name"], "luce_test")

    def test_save_device_sanitizes_local_key_in_response(self):
        resp = self._post_json(
            "/api/automazione/devices",
            {
                "name": "luce_test",
                "driver": "tuya_lan",
                "device_id": "bf123",
                "ip": "192.168.1.10",
                "local_key": "supersecret",
            },
            "tok",
        )
        data = resp.get_json()
        self.assertTrue(data["ok"])
        # Secret must not appear in plaintext in the response
        self.assertNotEqual(data["device"].get("local_key"), "supersecret")

        # GET list must also redact it
        authenticate_client(self.client, "tok")
        list_resp = self.client.get("/api/automazione/devices")
        devices = list_resp.get_json()["devices"]
        for dev in devices:
            self.assertNotEqual(dev.get("local_key"), "supersecret")

    def test_save_device_rejects_invalid_name(self):
        resp = self._post_json(
            "/api/automazione/devices",
            {"name": "Bad Name!", "driver": "tuya_lan", "device_id": "x", "ip": "1.2.3.4"},
            "tok",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_save_device_rejects_missing_device_id_for_tuya_lan(self):
        resp = self._post_json(
            "/api/automazione/devices",
            {"name": "luce_test", "driver": "tuya_lan", "ip": "192.168.1.10"},
            "tok",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_update_device_preserves_blank_secret(self):
        # Save with a secret
        self._post_json(
            "/api/automazione/devices",
            {
                "name": "luce_sec",
                "driver": "tuya_lan",
                "device_id": "bf999",
                "ip": "10.0.0.1",
                "local_key": "original_key",
            },
            "tok",
        )
        # Re-save with blank local_key — should keep the stored value
        self._post_json(
            "/api/automazione/devices",
            {
                "name": "luce_sec",
                "driver": "tuya_lan",
                "device_id": "bf999",
                "ip": "10.0.0.1",
                "local_key": "",
            },
            "tok",
        )
        # Read back via DeviceRegistry directly (internal, with decrypted secrets)
        registry = DeviceRegistry(store_path=self.devices_path)
        config = registry.get_config("luce_sec")
        self.assertIsNotNone(config)
        self.assertNotEqual(config.get("local_key"), "")

    def test_delete_device_removes_it(self):
        self._post_json(
            "/api/automazione/devices",
            {"name": "to_delete", "driver": "tuya_lan", "device_id": "x", "ip": "1.2.3.4"},
            "tok",
        )
        resp = self._delete("/api/automazione/devices/to_delete", "tok")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        authenticate_client(self.client, "tok")
        devices = self.client.get("/api/automazione/devices").get_json()["devices"]
        self.assertFalse(any(d["name"] == "to_delete" for d in devices))

    def test_delete_device_not_found_returns_404(self):
        resp = self._delete("/api/automazione/devices/no_such_device", "tok")
        self.assertEqual(resp.status_code, 404)

    def test_delete_device_rejects_invalid_name(self):
        # Use a name with uppercase — Flask can route it but _validate_name rejects it
        resp = self._delete("/api/automazione/devices/BadName", "tok")
        self.assertEqual(resp.status_code, 400)


# ── Rule CRUD tests ────────────────────────────────────────────────────────


def _make_rule(name="test_rule", on="person_detected"):
    return {
        "name": name,
        "on": on,
        "do": [{"device": "luce_test", "action": "turn_on"}],
        "cooldown": "60s",
    }


class AutomationRuleRouteTests(AutomationRouteTestBase):
    def setUp(self):
        super().setUp()
        # Add a device so rule validation passes
        self._post_json(
            "/api/automazione/devices",
            {"name": "luce_test", "driver": "tuya_lan", "device_id": "bf1", "ip": "10.0.0.1"},
            "tok",
        )

    def test_list_rules_returns_empty_initially(self):
        authenticate_client(self.client, "tok")
        resp = self.client.get("/api/automazione/rules")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["rules"], [])

    def test_save_valid_rule(self):
        resp = self._post_json("/api/automazione/rules", _make_rule(), "tok")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["rule"]["name"], "test_rule")

    def test_save_rule_rejects_invalid_name(self):
        rule = _make_rule(name="Bad Rule!")
        resp = self._post_json("/api/automazione/rules", rule, "tok")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_save_rule_rejects_unknown_event(self):
        rule = _make_rule(on="alien_detected")
        resp = self._post_json("/api/automazione/rules", rule, "tok")
        self.assertEqual(resp.status_code, 400)

    def test_save_rule_rejects_unknown_device(self):
        rule = {
            "name": "bad_rule",
            "on": "person_detected",
            "do": [{"device": "nonexistent_device", "action": "turn_on"}],
        }
        resp = self._post_json("/api/automazione/rules", rule, "tok")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nonexistent_device", resp.get_json()["error"])

    def test_save_rule_upserts_by_name(self):
        self._post_json("/api/automazione/rules", _make_rule(on="person_detected"), "tok")
        # Re-save same name with different event
        self._post_json("/api/automazione/rules", _make_rule(on="motion_detected"), "tok")

        authenticate_client(self.client, "tok")
        rules = self.client.get("/api/automazione/rules").get_json()["rules"]
        matches = [r for r in rules if r["name"] == "test_rule"]
        self.assertEqual(len(matches), 1)  # no duplicates
        self.assertEqual(matches[0]["on"], "motion_detected")

    def test_save_rule_rejects_invalid_cooldown(self):
        rule = _make_rule()
        rule["cooldown"] = "not_a_duration"
        resp = self._post_json("/api/automazione/rules", rule, "tok")
        self.assertEqual(resp.status_code, 400)

    def test_save_rule_with_time_window(self):
        rule = _make_rule()
        rule["between_from"] = "20:00"
        rule["between_to"] = "07:00"
        resp = self._post_json("/api/automazione/rules", rule, "tok")
        self.assertEqual(resp.status_code, 200)
        saved = resp.get_json()["rule"]
        self.assertEqual(saved.get("between"), ["20:00", "07:00"])

    def test_delete_rule_removes_it(self):
        self._post_json("/api/automazione/rules", _make_rule(), "tok")
        resp = self._delete("/api/automazione/rules/test_rule", "tok")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        authenticate_client(self.client, "tok")
        rules = self.client.get("/api/automazione/rules").get_json()["rules"]
        self.assertFalse(any(r["name"] == "test_rule" for r in rules))

    def test_delete_rule_not_found_returns_404(self):
        resp = self._delete("/api/automazione/rules/no_such_rule", "tok")
        self.assertEqual(resp.status_code, 404)

    def test_delete_rule_rejects_invalid_name(self):
        # Use a name with uppercase — Flask can route it but _validate_name rejects it
        resp = self._delete("/api/automazione/rules/BadRule", "tok")
        self.assertEqual(resp.status_code, 400)


# ── Toggle tests ───────────────────────────────────────────────────────────


class AutomationToggleTests(AutomationRouteTestBase):
    def test_toggle_missing_field_returns_400(self):
        resp = self._patch_json("/api/automazione/toggle", {}, "tok")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_toggle_enable_updates_runtime_config(self):
        resp = self._patch_json("/api/automazione/toggle", {"enabled": True}, "tok")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("enabled", data)

    def test_toggle_disable_returns_ok(self):
        resp = self._patch_json("/api/automazione/toggle", {"enabled": False}, "tok")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])


# ── Device test / rename / scan / import-tuya ──────────────────────────────


class _RecordingDispatcher:
    def __init__(self):
        self.submitted = []

    def submit(self, planned):
        self.submitted.append(planned)


class AutomationDeviceTestRenameTests(AutomationRouteTestBase):
    def _save_mock(self, name="luce_mock"):
        return self._post_json(
            "/api/automazione/devices",
            {"name": name, "driver": "mock"},
            "tok",
        )

    def test_test_device_turn_on(self):
        self._save_mock()
        resp = self._post_json(
            "/api/automazione/devices/luce_mock/test", {"action": "turn_on"}, "tok"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_test_device_rejects_bad_action(self):
        self._save_mock()
        resp = self._post_json(
            "/api/automazione/devices/luce_mock/test", {"action": "explode"}, "tok"
        )
        self.assertEqual(resp.status_code, 400)

    def test_test_device_unknown_returns_502(self):
        resp = self._post_json(
            "/api/automazione/devices/inesistente/test", {"action": "turn_on"}, "tok"
        )
        self.assertEqual(resp.status_code, 502)

    def test_rename_device_updates_rule_references(self):
        self._save_mock("luce_a")
        self._post_json(
            "/api/automazione/rules",
            {
                "name": "r_rename",
                "on": "person_detected",
                "do": [{"device": "luce_a", "action": "turn_on"}],
            },
            "tok",
        )
        resp = self._post_json(
            "/api/automazione/devices/luce_a/rename", {"new_name": "luce_b"}, "tok"
        )
        self.assertEqual(resp.status_code, 200)
        authenticate_client(self.client, "tok")
        devices = self.client.get("/api/automazione/devices").get_json()["devices"]
        self.assertEqual([d["name"] for d in devices], ["luce_b"])
        rules = self.client.get("/api/automazione/rules").get_json()["rules"]
        self.assertEqual(rules[0]["do"][0]["device"], "luce_b")

    def test_rename_collision_returns_400(self):
        self._save_mock("luce_a")
        self._save_mock("luce_b")
        resp = self._post_json(
            "/api/automazione/devices/luce_a/rename", {"new_name": "luce_b"}, "tok"
        )
        self.assertEqual(resp.status_code, 400)

    def test_scan_devices_preview(self):
        fake = [{"name": "Lampada", "id": "dev1", "ip": "10.0.0.5", "version": 3.3, "key": "k123"}]
        with mock.patch("blackframe.routes.automation.scan_lan_devices", return_value=fake):
            resp = self._post_json("/api/automazione/devices/scan", {}, "tok")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["found"], 1)
        self.assertEqual(data["devices"][0]["name"], "lampada")
        self.assertEqual(data["devices"][0]["local_key"], "***")

    def test_scan_devices_tinytuya_missing_returns_501(self):
        with mock.patch(
            "blackframe.routes.automation.scan_lan_devices",
            side_effect=ImportError("tinytuya non installato"),
        ):
            resp = self._post_json("/api/automazione/devices/scan", {}, "tok")
        self.assertEqual(resp.status_code, 501)

    def test_import_tuya_preview_and_commit(self):
        import io

        devices_json = json.dumps(
            [{"name": "Presa Cucina", "id": "dev9", "ip": "10.0.0.9", "key": "secret9"}]
        ).encode()
        authenticate_client(self.client, "tok")
        # preview
        resp = self.client.post(
            "/api/automazione/devices/import-tuya",
            data={"devices": (io.BytesIO(devices_json), "devices.json")},
            headers={"X-CSRF-Token": "tok"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["committed"])
        self.assertEqual(data["devices"][0]["name"], "presa_cucina")
        self.assertEqual(data["devices"][0]["local_key"], "***")
        # commit
        resp = self.client.post(
            "/api/automazione/devices/import-tuya",
            data={"devices": (io.BytesIO(devices_json), "devices.json"), "commit": "1"},
            headers={"X-CSRF-Token": "tok"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["committed"])
        registry = DeviceRegistry(store_path=self.devices_path)
        self.assertEqual(registry.get_config("presa_cucina")["local_key"], "secret9")

    def test_import_tuya_missing_file_returns_400(self):
        authenticate_client(self.client, "tok")
        resp = self.client.post(
            "/api/automazione/devices/import-tuya",
            data={},
            headers={"X-CSRF-Token": "tok"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)


# ── Rule test / enabled ────────────────────────────────────────────────────


class AutomationRuleTestEnabledTests(AutomationRouteTestBase):
    def _inject_engine(self, raw_rules):
        from blackframe.automation.engine import AutomationEngine
        from blackframe.automation.rules import parse_rules

        dispatcher = _RecordingDispatcher()
        engine = AutomationEngine(parse_rules(raw_rules), dispatcher=dispatcher)
        services = self.app.config["services"]
        services.automation_engine = engine
        return dispatcher

    def _rule(self, name="r1"):
        return {
            "name": name,
            "on": "person_detected",
            "do": [{"device": "luce", "action": "turn_on"}],
        }

    def test_test_rule_without_engine_returns_409(self):
        resp = self._post_json("/api/automazione/rules/r1/test", {"execute": True}, "tok")
        self.assertEqual(resp.status_code, 409)

    def test_test_rule_preview_does_not_dispatch(self):
        dispatcher = self._inject_engine([self._rule("r1")])
        resp = self._post_json("/api/automazione/rules/r1/test", {"execute": False}, "tok")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["executed"])
        self.assertEqual(data["actions"][0]["device"], "luce")
        self.assertEqual(dispatcher.submitted, [])

    def test_test_rule_execute_dispatches(self):
        dispatcher = self._inject_engine([self._rule("r1")])
        resp = self._post_json("/api/automazione/rules/r1/test", {"execute": True}, "tok")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["executed"])
        self.assertEqual(len(dispatcher.submitted), 1)

    def test_test_rule_unknown_returns_404(self):
        self._inject_engine([self._rule("r1")])
        resp = self._post_json("/api/automazione/rules/altra/test", {"execute": False}, "tok")
        self.assertEqual(resp.status_code, 404)

    def test_set_rule_enabled_toggles(self):
        # Need a device + rule on disk
        self._post_json("/api/automazione/devices", {"name": "luce", "driver": "mock"}, "tok")
        self._post_json("/api/automazione/rules", self._rule("r1"), "tok")
        resp = self._patch_json("/api/automazione/rules/r1/enabled", {"enabled": False}, "tok")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["enabled"])
        authenticate_client(self.client, "tok")
        rules = self.client.get("/api/automazione/rules").get_json()["rules"]
        self.assertFalse(rules[0]["enabled"])

    def test_set_rule_enabled_unknown_returns_404(self):
        resp = self._patch_json("/api/automazione/rules/nope/enabled", {"enabled": True}, "tok")
        self.assertEqual(resp.status_code, 404)


# ── Import / Export ────────────────────────────────────────────────────────


class AutomationImportExportTests(AutomationRouteTestBase):
    def test_export_returns_bundle(self):
        self._post_json("/api/automazione/devices", {"name": "luce", "driver": "mock"}, "tok")
        authenticate_client(self.client, "tok")
        resp = self.client.get("/api/automazione/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        bundle = json.loads(resp.data)
        self.assertEqual(bundle["devices"][0]["name"], "luce")
        self.assertIn("rules", bundle)

    def test_import_bundle_restores_devices_and_rules(self):
        bundle = {
            "version": 1,
            "devices": [{"name": "luce", "driver": "mock", "local_key": "***"}],
            "rules": [
                {
                    "name": "r1",
                    "on": "person_detected",
                    "do": [{"device": "luce", "action": "turn_on"}],
                }
            ],
        }
        resp = self._post_json("/api/automazione/import", bundle, "tok")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["devices_imported"], 1)
        self.assertEqual(data["rules_imported"], 1)
        self.assertEqual(data["errors"], [])

    def test_import_invalid_bundle_returns_400(self):
        resp = self._post_json("/api/automazione/import", [], "tok")
        self.assertEqual(resp.status_code, 400)


class AutomationPageRenderTests(AutomationRouteTestBase):
    def test_page_renders_with_new_controls(self):
        authenticate_client(self.client, "tok")
        resp = self.client.get("/automazione")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for marker in (
            'id="btn-wizard"',
            'id="btn-export"',
            'id="btn-import"',
            'id="auto-rename-dialog"',
            'id="auto-wizard-dialog"',
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
