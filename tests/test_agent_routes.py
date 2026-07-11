import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blackframe.agent import AgentService, AgentTranscriptStore

# ── Shared test helpers (stesso pattern di tests/test_automation_routes.py) ──


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


# ── Fakes minimi ──────────────────────────────────────────────────────────


class FakeCamera:
    def get_frame(self):
        return None

    def get_status(self):
        return {"connected": False, "connection_state": "offline"}


class FakePtz:
    def get_status(self):
        return {"available": False, "error": ""}


class FakeMotion:
    config = {"classification_enabled": False, "record_enabled": False}
    classifier = None

    def get_status(self):
        return {"enabled": False, "motion_detected": False, "last_motion_at": None}

    def list_events(self, limit=8, include_frames=False):
        return []


class FakeRuntimeConfig:
    def __init__(self):
        self._vals = {"AGENT_ENABLED": True}

    def get_public_config(self):
        return {}

    def update(self, updates, allow_sensitive=False, allow_internal=False):
        # Mirror RuntimeConfigManager.update(), che scrive anche su os.environ:
        # _build_agent()/reload_agent() rileggono AGENT_ENABLED dall'env.
        self._vals.update(updates)
        for key, value in updates.items():
            os.environ[key] = "true" if value is True else "false" if value is False else str(value)
        return {}


# ── Base class ───────────────────────────────────────────────────────────


class AgentRouteTestBase(unittest.TestCase):
    def setUp(self):
        from blackframe.auth import rate_limiter

        rate_limiter._events.clear()

        self.tmp = tempfile.TemporaryDirectory()
        self.app_module = load_app_module()
        features = self.app_module.FeatureServices(
            presets=self.app_module.PresetService("data/test-presets.json"),
            notifications=self.app_module.NotificationService(),
            recording=self.app_module.RecordingService("captures/test-recordings"),
            camera_profiles=self.app_module.CameraProfileService("data/test-camera-profiles.json"),
            wifi=self.app_module.WifiService(),
        )
        self.runtime_config = FakeRuntimeConfig()
        self.services = self.app_module.AppServices(
            camera=FakeCamera(),
            ptz=FakePtz(),
            motion=FakeMotion(),
            features=features,
            runtime_config=self.runtime_config,
        )
        self.services.agent = AgentService(self.services)
        # Transcript su file temporaneo: mai scrivere in data/ del repo.
        self.services.agent_transcript = AgentTranscriptStore(
            Path(self.tmp.name) / "transcript.json"
        )

        self.env_patch = mock.patch.dict(os.environ, {"AGENT_ENABLED": "true"})
        self.env_patch.start()
        self.app = self.app_module.create_app(self.services)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def _post_json(self, url, data, token="tok", authed=True):
        if authed:
            authenticate_client(self.client, token)
        return self.client.post(
            url, data=json.dumps(data), headers=csrf_headers(token), content_type="application/json"
        )

    def _patch_json(self, url, data, token="tok", authed=True):
        if authed:
            authenticate_client(self.client, token)
        return self.client.patch(
            url, data=json.dumps(data), headers=csrf_headers(token), content_type="application/json"
        )


# ── Auth ─────────────────────────────────────────────────────────────────


class AgentRoutesAuthTests(AgentRouteTestBase):
    def test_page_requires_auth(self):
        # Route pagina (require_auth non-api): redirige al login, non 401.
        resp = self.client.get("/agente")
        self.assertEqual(resp.status_code, 302)

    def test_interpret_requires_auth(self):
        resp = self._post_json("/api/agente/interpret", {"text": "ciao"}, authed=False)
        self.assertEqual(resp.status_code, 401)

    def test_inventory_requires_auth(self):
        resp = self.client.get("/api/agente/inventory")
        self.assertEqual(resp.status_code, 401)

    def test_inventory_is_redacted(self):
        class Registry:
            def list_devices(self):
                return [
                    {
                        "name": "luce_studio",
                        "driver": "test",
                        "local_key": "must-not-leak",
                        "ip": "192.0.2.10",
                    }
                ]

        self.services.automation_registry = Registry()
        authenticate_client(self.client)

        resp = self.client.get("/api/agente/inventory")
        payload = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("luce_studio", str(payload))
        self.assertNotIn("must-not-leak", str(payload))
        self.assertNotIn("192.0.2.10", str(payload))

    def test_entity_state_requires_auth_and_rejects_unknown_entity(self):
        unauthenticated = self.client.get("/api/agente/entities/light.unknown/state")
        self.assertEqual(unauthenticated.status_code, 401)

        authenticate_client(self.client)
        missing = self.client.get("/api/agente/entities/light.unknown/state")
        self.assertEqual(missing.status_code, 404)


# ── Interpret / confirm / cancel ───────────────────────────────────────────


class AgentInterpretTests(AgentRouteTestBase):
    def test_empty_text_rejected(self):
        resp = self._post_json("/api/agente/interpret", {"text": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_agent_disabled_returns_503(self):
        self.services.agent = None
        resp = self._post_json("/api/agente/interpret", {"text": "stato"})
        self.assertEqual(resp.status_code, 503)

    def test_readonly_command_executes_immediately(self):
        # chat_text -> None: composizione naturale fallita, fail-open
        # sull'output grezzo del comando.
        with (
            mock.patch(
                "blackframe.agent.intent.ollama_client.chat_json",
                return_value={"command": "status", "arg": None},
            ),
            mock.patch(
                "blackframe.agent.answer.ollama_client.chat_text",
                return_value=None,
            ),
        ):
            resp = self._post_json("/api/agente/interpret", {"text": "come sta la camera?"})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["executed"])
        self.assertIsNone(data.get("pending_id"))
        self.assertIn("BLACKFRAME", data["result_text"])

    def test_readonly_question_returns_composed_answer(self):
        with (
            mock.patch(
                "blackframe.agent.intent.ollama_client.chat_json",
                return_value={"command": "status", "arg": None},
            ),
            mock.patch(
                "blackframe.agent.answer.ollama_client.chat_text",
                return_value="La telecamera funziona e il movimento è attivo.",
            ),
        ):
            resp = self._post_json("/api/agente/interpret", {"text": "come sta la camera?"})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["result_text"], "La telecamera funziona e il movimento è attivo.")

    def test_state_change_command_requires_confirmation(self):
        with mock.patch(
            "blackframe.agent.intent.ollama_client.chat_json",
            return_value={"command": "motion_off", "arg": None},
        ):
            resp = self._post_json("/api/agente/interpret", {"text": "spegni il movimento"})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["executed"])
        self.assertIsNotNone(data["pending_id"])

        confirm_resp = self._post_json("/api/agente/confirm", {"pending_id": data["pending_id"]})
        confirm_data = confirm_resp.get_json()
        self.assertTrue(confirm_data["ok"])
        self.assertTrue(confirm_data["executed"])

    def test_confirm_unknown_pending_id_returns_error(self):
        resp = self._post_json("/api/agente/confirm", {"pending_id": "does-not-exist"})
        data = resp.get_json()
        self.assertFalse(data["ok"])

    def test_cancel_prevents_confirmation(self):
        with mock.patch(
            "blackframe.agent.intent.ollama_client.chat_json",
            return_value={"command": "motion_off", "arg": None},
        ):
            resp = self._post_json("/api/agente/interpret", {"text": "spegni il movimento"})
        pending_id = resp.get_json()["pending_id"]

        cancel_resp = self._post_json("/api/agente/cancel", {"pending_id": pending_id})
        self.assertTrue(cancel_resp.get_json()["ok"])

        confirm_resp = self._post_json("/api/agente/confirm", {"pending_id": pending_id})
        self.assertFalse(confirm_resp.get_json()["ok"])

    def test_media_command_excluded_on_web_channel(self):
        with mock.patch(
            "blackframe.agent.intent.ollama_client.chat_json",
            return_value={"command": "snapshot", "arg": None},
        ):
            resp = self._post_json("/api/agente/interpret", {"text": "mandami una foto"})
        data = resp.get_json()
        self.assertFalse(data["ok"])

    def test_interpret_records_transcript(self):
        with (
            mock.patch(
                "blackframe.agent.intent.ollama_client.chat_json",
                return_value={"command": "status", "arg": None},
            ),
            mock.patch(
                "blackframe.agent.answer.ollama_client.chat_text",
                return_value=None,
            ),
        ):
            self._post_json("/api/agente/interpret", {"text": "come sta la camera?"})
        messages = self.services.agent_transcript.list()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["text"], "come sta la camera?")
        self.assertEqual(messages[1]["role"], "agent")
        self.assertEqual(messages[1]["command"], "status")

    def test_confirm_flow_records_request_and_outcome(self):
        with mock.patch(
            "blackframe.agent.intent.ollama_client.chat_json",
            return_value={"command": "motion_off", "arg": None},
        ):
            resp = self._post_json("/api/agente/interpret", {"text": "spegni il movimento"})
        pending_id = resp.get_json()["pending_id"]
        self._post_json("/api/agente/confirm", {"pending_id": pending_id})

        kinds = [m["kind"] for m in self.services.agent_transcript.list()]
        self.assertIn("confirm_request", kinds)
        self.assertIn("executed", kinds)

    def test_history_endpoint_returns_messages(self):
        self.services.agent_transcript.append("user", "ciao")
        self.services.agent_transcript.append("agent", "ciao a te")
        authenticate_client(self.client)
        resp = self.client.get("/api/agente/history")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual([m["text"] for m in data["messages"]], ["ciao", "ciao a te"])

    def test_history_requires_auth(self):
        resp = self.client.get("/api/agente/history")
        self.assertEqual(resp.status_code, 401)

    def test_history_delete_clears_transcript(self):
        self.services.agent_transcript.append("user", "ciao")
        token = authenticate_client(self.client)
        resp = self.client.delete("/api/agente/history", headers=csrf_headers(token))
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(self.services.agent_transcript.list(), [])

    def test_toggle_updates_runtime_config_and_reloads_agent(self):
        resp = self._patch_json("/api/agente/toggle", {"enabled": False})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["enabled"])
        self.assertEqual(self.runtime_config._vals["AGENT_ENABLED"], False)


if __name__ == "__main__":
    unittest.main()
