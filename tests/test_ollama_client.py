import json
import unittest
from unittest import mock

from blackframe.agent import ollama_client


class ChatJsonTests(unittest.TestCase):
    def _fake_response(self, content):
        body = json.dumps({"message": {"content": json.dumps(content)}}).encode("utf-8")

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        return FakeResp()

    def test_keep_alive_included_in_payload_when_set(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return self._fake_response({"command": "status", "arg": None})

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            ollama_client.chat_json("http://x", "m", "sys", "user", timeout=1.0, keep_alive="30m")
        self.assertEqual(captured["payload"]["keep_alive"], "30m")

    def test_keep_alive_omitted_when_not_set(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return self._fake_response({"command": "status", "arg": None})

        with mock.patch.object(ollama_client.urllib.request, "urlopen", fake_urlopen):
            ollama_client.chat_json("http://x", "m", "sys", "user", timeout=1.0)
        self.assertNotIn("keep_alive", captured["payload"])
