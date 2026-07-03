import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from blackframe.agent.transcript import AgentTranscriptStore


class AgentTranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "transcript.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, **kwargs):
        return AgentTranscriptStore(self.path, **kwargs)

    def test_append_list_roundtrip(self):
        store = self._store()
        store.append("user", "accendi la lampada")
        store.append("agent", "Ho capito: accendi — confermi?", kind="confirm_request")
        messages = store.list()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["kind"], "confirm_request")
        self.assertTrue(all("id" in m and "ts" in m for m in messages))

    def test_rotation_keeps_only_last_max_messages(self):
        store = self._store(max_messages=3)
        for i in range(6):
            store.append("user", f"msg-{i}")
        texts = [m["text"] for m in store.list()]
        self.assertEqual(texts, ["msg-3", "msg-4", "msg-5"])

    def test_reopening_reads_persisted_messages(self):
        self._store().append("user", "persistito")
        texts = [m["text"] for m in self._store().list()]
        self.assertEqual(texts, ["persistito"])

    def test_corrupted_file_reinitializes_empty(self):
        self.path.write_text("{ non json", encoding="utf-8")
        store = self._store()
        self.assertEqual(store.list(), [])
        store.append("user", "riparte")
        self.assertEqual(len(self._store().list()), 1)

    def test_clear_removes_all(self):
        store = self._store()
        store.append("user", "ciao")
        store.clear()
        self.assertEqual(store.list(), [])
        self.assertEqual(self._store().list(), [])

    def test_file_written_private(self):
        self._store().append("user", "segreto in chiaro")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_file_schema_versioned(self):
        self._store().append("user", "ciao")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertIsInstance(payload["messages"], list)

    def test_list_limit(self):
        store = self._store()
        for i in range(5):
            store.append("user", f"m{i}")
        self.assertEqual([m["text"] for m in store.list(limit=2)], ["m3", "m4"])


if __name__ == "__main__":
    unittest.main()
