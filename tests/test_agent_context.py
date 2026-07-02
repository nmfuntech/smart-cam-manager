import os
import time
import unittest
from unittest import mock

from blackframe.agent.context import ConversationContextStore
from blackframe.agent.intent import Suggestion
from blackframe.agent.service import AgentService
from blackframe.commands import CommandResult


class ContextStoreTests(unittest.TestCase):
    def test_set_get_roundtrip(self):
        store = ConversationContextStore(ttl_seconds=60)
        store.set("telegram", "123", "accendi la lampada", "device_on", "lampada_ingresso")
        turn = store.get("telegram", "123")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.command, "device_on")
        self.assertEqual(turn.arg, "lampada_ingresso")

    def test_expired_turn_is_purged(self):
        store = ConversationContextStore(ttl_seconds=0.01)
        store.set("telegram", "123", "stato", "status", None)
        time.sleep(0.02)
        self.assertIsNone(store.get("telegram", "123"))

    def test_channel_and_key_isolation(self):
        store = ConversationContextStore(ttl_seconds=60)
        store.set("telegram", "123", "stato", "status", None)
        self.assertIsNone(store.get("telegram", "456"))
        self.assertIsNone(store.get("web", "123"))

    def test_ttl_zero_disables_store(self):
        store = ConversationContextStore(ttl_seconds=0)
        self.assertFalse(store.enabled)
        store.set("telegram", "123", "stato", "status", None)
        self.assertIsNone(store.get("telegram", "123"))

    def test_ttl_read_from_env(self):
        with mock.patch.dict(os.environ, {"AGENT_CONTEXT_TTL_SEC": "0"}):
            self.assertFalse(ConversationContextStore().enabled)


class ServiceContextTests(unittest.TestCase):
    """Il turno si salva alla proposta (anche pending) e i fallimenti non
    sovrascrivono il contesto buono."""

    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"AGENT_ENABLED": "true"})
        self.env_patch.start()
        self.agent = AgentService(mock.Mock())

    def tearDown(self):
        self.env_patch.stop()

    def test_pending_proposal_stores_turn_and_next_call_receives_it(self):
        captured = {}

        def fake_interpret(text, exclude=frozenset(), services=None, last_turn=None):
            captured["last_turn"] = last_turn
            return Suggestion(ok=True, command="device_on", arg="lampada_ingresso")

        with mock.patch("blackframe.agent.service.interpret", side_effect=fake_interpret):
            first = self.agent.propose("accendi la lampada", "telegram", "1")
            self.assertIsNotNone(first.pending_id)
            self.assertIsNone(captured["last_turn"])

            self.agent.propose("ora spegnila", "telegram", "1")
        turn = captured["last_turn"]
        self.assertIsNotNone(turn)
        self.assertEqual(turn.user_text, "accendi la lampada")
        self.assertEqual(turn.command, "device_on")

    def test_failed_interpretation_does_not_overwrite_context(self):
        responses = [
            Suggestion(ok=True, command="device_on", arg="lampada_ingresso"),
            Suggestion(ok=False, reason="Non ho capito."),
        ]
        captured = {}

        def fake_interpret(text, exclude=frozenset(), services=None, last_turn=None):
            captured["last_turn"] = last_turn
            return responses.pop(0) if responses else Suggestion(ok=False, reason="fine")

        with mock.patch("blackframe.agent.service.interpret", side_effect=fake_interpret):
            self.agent.propose("accendi la lampada", "telegram", "1")
            self.agent.propose("gnegnegne", "telegram", "1")
            self.agent.propose("terzo messaggio", "telegram", "1")
        turn = captured["last_turn"]
        self.assertIsNotNone(turn)
        self.assertEqual(turn.command, "device_on")

    def test_readonly_execution_stores_turn(self):
        def fake_interpret(text, exclude=frozenset(), services=None, last_turn=None):
            return Suggestion(ok=True, command="status", arg=None)

        with (
            mock.patch("blackframe.agent.service.interpret", side_effect=fake_interpret),
            mock.patch(
                "blackframe.agent.service.registry_execute",
                return_value=CommandResult(text="tutto ok"),
            ),
        ):
            self.agent.propose("stato", "web", "sess")
        turn = self.agent._context.get("web", "sess")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.command, "status")


if __name__ == "__main__":
    unittest.main()
