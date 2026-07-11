import json
import socket
import unittest
import urllib.error
from unittest import mock

from blackframe.agent import ollama_client


def _fake_response(content):
    if not isinstance(content, str):
        content = json.dumps(content)
    body = json.dumps({"message": {"content": content}}).encode("utf-8")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    return FakeResp()


class ChatJsonTests(unittest.TestCase):
    def _capture_call(self, **kwargs):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            captured["url"] = req.full_url
            return _fake_response({"command": "status", "arg": None})

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            ollama_client.chat_json(
                "http://127.0.0.1", "m", "sys", "user", timeout=1.0, **kwargs
            )
        return captured

    def test_keep_alive_included_in_payload_when_set(self):
        captured = self._capture_call(keep_alive="30m")
        self.assertEqual(captured["payload"]["keep_alive"], "30m")

    def test_keep_alive_omitted_when_not_set(self):
        captured = self._capture_call()
        self.assertNotIn("keep_alive", captured["payload"])

    def test_format_defaults_to_json(self):
        captured = self._capture_call()
        self.assertEqual(captured["payload"]["format"], "json")

    def test_response_schema_used_as_format(self):
        schema = {"type": "object", "properties": {"command": {"enum": ["status"]}}}
        captured = self._capture_call(response_schema=schema)
        self.assertEqual(captured["payload"]["format"], schema)

    def test_history_messages_between_system_and_user(self):
        history = [
            {"role": "user", "content": "esempio"},
            {"role": "assistant", "content": '{"command": "status", "arg": null}'},
        ]
        captured = self._capture_call(history=history)
        roles = [m["role"] for m in captured["payload"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(captured["payload"]["messages"][-1]["content"], "user")

    def test_options_merged_over_temperature_default(self):
        captured = self._capture_call(options={"num_ctx": 1536, "temperature": 0.2})
        self.assertEqual(captured["payload"]["options"], {"temperature": 0.2, "num_ctx": 1536})

    def test_http_400_with_schema_falls_back_to_plain_json(self):
        payloads = []

        def fake_urlopen(req, timeout):
            payloads.append(json.loads(req.data))
            if len(payloads) == 1:
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, None)
            return _fake_response({"command": "status", "arg": None})

        schema = {"type": "object"}
        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            result = ollama_client.chat_json(
                "http://127.0.0.1", "m", "sys", "user", timeout=1.0, response_schema=schema
            )
        self.assertEqual(result, {"command": "status", "arg": None})
        self.assertEqual(payloads[0]["format"], schema)
        self.assertEqual(payloads[1]["format"], "json")

    def test_no_retry_on_timeout(self):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(1)
            raise urllib.error.URLError(socket.timeout("timed out"))

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            result = ollama_client.chat_json(
                "http://127.0.0.1", "m", "sys", "user", timeout=1.0
            )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)

    def test_single_retry_on_connection_refused(self):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.URLError(ConnectionRefusedError("refused"))
            return _fake_response({"command": "status", "arg": None})

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            result = ollama_client.chat_json(
                "http://127.0.0.1", "m", "sys", "user", timeout=1.0
            )
        self.assertEqual(result, {"command": "status", "arg": None})
        self.assertEqual(len(calls), 2)


class ChatTextTests(unittest.TestCase):
    def test_returns_stripped_text_without_format(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("  Risposta naturale.  ")

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            result = ollama_client.chat_text(
                "http://127.0.0.1", "m", "sys", "user", timeout=1.0
            )
        self.assertEqual(result, "Risposta naturale.")
        self.assertNotIn("format", captured["payload"])

    def test_none_on_error(self):
        def fake_urlopen(req, timeout):
            raise urllib.error.URLError(socket.timeout("timed out"))

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            self.assertIsNone(
                ollama_client.chat_text(
                    "http://127.0.0.1", "m", "sys", "user", timeout=1.0
                )
            )

    def test_remote_endpoint_is_blocked_by_default(self):
        with mock.patch.object(ollama_client.urllib.request, "urlopen") as urlopen:
            result = ollama_client.chat_text("http://192.168.1.10:11434", "m", "s", "u")
        self.assertIsNone(result)
        urlopen.assert_not_called()


class WarmupTests(unittest.TestCase):
    def test_posts_generate_with_model_and_keep_alive(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return _fake_response("")

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            ollama_client.warmup("http://127.0.0.1", "m", keep_alive="30m")
        self.assertTrue(captured["url"].endswith("/api/generate"))
        self.assertEqual(captured["payload"], {"model": "m", "keep_alive": "30m"})

    def test_errors_are_swallowed(self):
        def fake_urlopen(req, timeout):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            ollama_client.warmup("http://127.0.0.1", "m")  # non deve sollevare


if __name__ == "__main__":
    unittest.main()
