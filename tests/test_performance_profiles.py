import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from flask import Flask

from blackframe.auth import auth_bp, configure_auth, rate_limiter
from blackframe.performance_profiles import PerformanceProfileManager
from blackframe.routes.settings import settings_bp
from blackframe.runtime_config import RuntimeConfigManager


def _catalog(profiles=None):
    return {
        "version": 1,
        "profiles": profiles
        or {
            "eco": {
                "label": "Eco",
                "description": "Basso consumo",
                "requirements": {
                    "min_ram_gb": 2,
                    "recommended_ram_gb": 4,
                    "min_cpu_threads": 2,
                    "max_monitored_cameras": 1,
                },
                "settings": {
                    "MOTION_THRESHOLD": 40,
                    "RECORD_FPS": 5,
                    "APP_GUNICORN_THREADS": 3,
                },
            },
            "balanced": {
                "label": "Bilanciato",
                "description": "Uso normale",
                "requirements": {
                    "min_ram_gb": 4,
                    "recommended_ram_gb": 8,
                    "min_cpu_threads": 4,
                    "max_monitored_cameras": 2,
                },
                "settings": {
                    "MOTION_THRESHOLD": 30,
                    "RECORD_FPS": 6,
                    "APP_GUNICORN_THREADS": 4,
                },
            },
            "performance": {
                "label": "Prestazioni",
                "description": "Massima fluidità",
                "requirements": {
                    "min_ram_gb": 8,
                    "recommended_ram_gb": 16,
                    "min_cpu_threads": 8,
                    "max_monitored_cameras": 4,
                },
                "settings": {
                    "MOTION_THRESHOLD": 25,
                    "RECORD_FPS": 10,
                    "APP_GUNICORN_THREADS": 8,
                },
            },
        },
    }


class PerformanceProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.catalog_path = root / "profiles.yaml"
        self.state_path = root / "state.json"
        self.env_path = root / ".env"
        self.env_path.write_text("MOTION_THRESHOLD=55\nRECORD_FPS=8\nAPP_GUNICORN_THREADS=6\n")
        self.catalog_path.write_text(yaml.safe_dump(_catalog(), sort_keys=False))
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "MOTION_THRESHOLD": "55",
                "RECORD_FPS": "8",
                "APP_GUNICORN_THREADS": "6",
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.runtime = RuntimeConfigManager(self.env_path)

    def _manager(self, hardware=None):
        return PerformanceProfileManager(
            self.runtime,
            self.catalog_path,
            self.state_path,
            hardware_provider=lambda: hardware
            or {"ram_gb": 4.0, "cpu_threads": 4, "architecture": "x64", "platform": "test"},
        )

    def test_recommends_highest_compatible_profile(self):
        result = self._manager().list_profiles(camera_count=1)
        self.assertEqual(result["recommended"], "balanced")
        performance = next(p for p in result["profiles"] if p["name"] == "performance")
        self.assertFalse(performance["compatibility"]["compatible"])

    def test_preview_returns_only_differences_and_restart_flags(self):
        result = self._manager().preview("balanced")
        by_key = {item["key"]: item for item in result["changes"]}
        self.assertEqual(by_key["MOTION_THRESHOLD"]["current"], 55)
        self.assertTrue(by_key["APP_GUNICORN_THREADS"]["requires_restart"])

    def test_apply_writes_env_and_private_state(self):
        result = self._manager().apply("eco")
        state = json.loads(self.state_path.read_text())

        self.assertEqual(result["profile"], "eco")
        self.assertIn("MOTION_THRESHOLD=40", self.env_path.read_text())
        self.assertEqual(state["profile"], "eco")
        self.assertEqual(state["overrides"], {})
        if os.name != "nt":
            self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o600)

    def test_manual_change_marks_profile_customized(self):
        manager = self._manager()
        manager.apply("eco")
        self.runtime.update({"MOTION_THRESHOLD": 60})
        manager.record_overrides({"MOTION_THRESHOLD": 60})

        result = manager.list_profiles()

        self.assertTrue(result["customized"])
        self.assertEqual(result["overrides"], {"MOTION_THRESHOLD": 60})

    def test_return_to_baseline_removes_override(self):
        manager = self._manager()
        manager.apply("eco")
        manager.record_overrides({"MOTION_THRESHOLD": 60})
        manager.record_overrides({"MOTION_THRESHOLD": 40})
        self.assertFalse(manager.list_profiles()["customized"])

    def test_runtime_drift_is_detected_without_explicit_override_hook(self):
        manager = self._manager()
        manager.apply("eco")
        self.runtime.update({"MOTION_THRESHOLD": 61})
        result = manager.list_profiles()
        self.assertTrue(result["customized"])
        self.assertEqual(result["overrides"]["MOTION_THRESHOLD"], 61)

    def test_exact_environment_can_infer_active_profile_without_state(self):
        manager = self._manager()
        settings = manager._load_catalog()["profiles"]["balanced"]["settings"]
        self.runtime.update(settings, allow_internal=True)
        result = manager.list_profiles()
        self.assertEqual(result["active"], "balanced")
        self.assertTrue(result["inferred"])

    def test_state_failure_rolls_back_environment_and_env_file(self):
        manager = self._manager()
        before = self.env_path.read_bytes()
        with mock.patch.object(manager, "_write_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.apply("eco")
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertEqual(os.environ["MOTION_THRESHOLD"], "55")

    def test_catalog_cannot_set_unregistered_secret(self):
        data = _catalog()
        data["profiles"]["eco"]["settings"]["APP_SECRET_KEY"] = "stolen"
        self.catalog_path.write_text(yaml.safe_dump(data, sort_keys=False))
        with self.assertRaisesRegex(ValueError, "non modificabile"):
            self._manager().list_profiles()


class PerformanceProfileRouteTests(unittest.TestCase):
    def setUp(self):
        rate_limiter._events.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.catalog_path = root / "profiles.yaml"
        self.state_path = root / "state.json"
        self.env_path = root / ".env"
        self.catalog_path.write_text(yaml.safe_dump(_catalog(), sort_keys=False))
        self.env_path.write_text("MOTION_THRESHOLD=55\nRECORD_FPS=8\nAPP_GUNICORN_THREADS=6\n")
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "APP_ADMIN_PASSWORD": "test-pass",
                "APP_SECRET_KEY": "test-secret",
                "APP_BIND_HOST": "127.0.0.1",
                "MOTION_THRESHOLD": "55",
                "RECORD_FPS": "8",
                "APP_GUNICORN_THREADS": "6",
                "PERFORMANCE_PROFILE_CATALOG_PATH": str(self.catalog_path),
                "PERFORMANCE_PROFILE_STATE_PATH": str(self.state_path),
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.runtime = RuntimeConfigManager(self.env_path)
        self.applied = []
        self.agent = SimpleNamespace(start_warmup=mock.Mock())
        services = SimpleNamespace(
            runtime_config=self.runtime,
            monitors={},
            agent=self.agent,
            apply_runtime_config_all=lambda updates: self.applied.append(dict(updates)),
        )
        app = Flask(__name__)
        configure_auth(app)
        app.config["services"] = services
        app.register_blueprint(auth_bp)
        app.register_blueprint(settings_bp)
        self.client = app.test_client()

    def _auth(self):
        with self.client.session_transaction() as session:
            session["blackframe_auth_user"] = "admin"
            session["blackframe_csrf_token"] = "csrf"

    def test_list_requires_auth(self):
        self.assertEqual(self.client.get("/api/performance_profiles").status_code, 401)

    def test_preview_requires_csrf(self):
        self._auth()
        response = self.client.post(
            "/api/performance_profiles/preview",
            json={"profile": "eco"},
        )
        self.assertEqual(response.status_code, 403)

    def test_apply_profile_propagates_validated_updates(self):
        self._auth()
        response = self.client.post(
            "/api/performance_profiles/apply",
            json={"profile": "balanced"},
            headers={"X-CSRF-Token": "csrf"},
        )
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.applied[0]["MOTION_THRESHOLD"], 30)
        self.assertIn("APP_GUNICORN_THREADS", data["restart_required"])
        self.agent.start_warmup.assert_called_once()

    def test_unknown_profile_is_rejected(self):
        self._auth()
        response = self.client.post(
            "/api/performance_profiles/apply",
            json={"profile": "../../evil"},
            headers={"X-CSRF-Token": "csrf"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
