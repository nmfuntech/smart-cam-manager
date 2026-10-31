import os
import unittest
from types import SimpleNamespace
from unittest import mock

from blackframe.agent import AgentService, message_log


class MessageLogTests(unittest.TestCase):
    def _service(self):
        return AgentService(SimpleNamespace())

    def test_readonly_proposal_is_logged_with_source(self):
        service = self._service()
        with mock.patch.dict(os.environ, {"AGENT_ENABLED": "true"}):
            with mock.patch("blackframe.agent.service.registry_execute", return_value=None):
                service.propose("stato", "telegram", "1")
        (entry,) = message_log.read_all()
        self.assertEqual(entry["event"], "proposal")
        self.assertEqual(entry["text"], "stato")
        self.assertEqual(entry["command"], "status")
        self.assertEqual(entry["source"], "fastpath")

    def test_cancelled_proposal_is_traceable(self):
        service = self._service()
        with mock.patch.dict(os.environ, {"AGENT_ENABLED": "true"}):
            result = service.propose("metti la modalità casa", "telegram", "1")
            self.assertTrue(service.cancel(result.pending_id, "telegram", "1"))
        events = [(e["event"], e.get("pending_id")) for e in message_log.read_all()]
        self.assertEqual(events, [("proposal", result.pending_id), ("cancel", result.pending_id)])

    def test_disabled_log_writes_nothing(self):
        with mock.patch.dict(os.environ, {"AGENT_MESSAGE_LOG": "false"}):
            message_log.record("proposal", text="x")
        self.assertEqual(message_log.read_all(), [])

    def test_trim_keeps_newest_lines(self):
        with mock.patch.dict(os.environ, {"AGENT_MESSAGE_LOG_MAX": "100"}):
            for i in range(250):
                message_log.record("proposal", text=str(i))
        texts = [e["text"] for e in message_log.read_all()]
        self.assertLessEqual(len(texts), 200)
        self.assertEqual(texts[-1], "249")


class ExportTests(unittest.TestCase):
    def test_build_cases_tags_confirm_and_cancel(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "export_agent_messages.py"
        spec = importlib.util.spec_from_file_location("export_agent_messages", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entries = [
            {"event": "proposal", "text": "arma", "command": "arma", "ok": True,
             "pending_id": "p1", "source": "fastpath"},
            {"event": "confirm", "pending_id": "p1"},
            {"event": "proposal", "text": "il cane?", "command": "detect_pet_on", "ok": True,
             "pending_id": "p2", "source": "llm"},
            {"event": "cancel", "pending_id": "p2"},
            {"event": "proposal", "text": "pizza", "command": None, "ok": False, "source": "gate"},
        ]  # fmt: skip
        tags = {c["text"]: c["tags"][1] for c in module.build_cases(entries)}
        self.assertEqual(tags, {"arma": "confirmed", "il cane?": "cancelled", "pizza": "rejected"})
