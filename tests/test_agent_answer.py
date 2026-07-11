import os
import unittest
from unittest import mock

from blackframe.agent import answer
from blackframe.agent.intent import Suggestion
from blackframe.agent.service import AgentService
from blackframe.commands import CommandResult


class LooksLikeQuestionTests(unittest.TestCase):
    def test_positives(self):
        for text in (
            "le luci sono accese?",
            "le luci sono accese",
            "il movimento è attivo",
            "quanti eventi ci sono",
            "come sta la camera",
            "c'è stato movimento oggi?",
            "cosa è successo?",
        ):
            self.assertTrue(answer.looks_like_question(text), text)

    def test_negatives(self):
        for text in ("stato", "accendi la lampada", "spegni tutto", "eventi", ""):
            self.assertFalse(answer.looks_like_question(text), text)


class ComposeAnswerTests(unittest.TestCase):
    def test_result_text_is_truncated(self):
        captured = {}

        def fake_chat_text(base_url, model, system_prompt, user_text, **kwargs):
            captured["user_text"] = user_text
            return "Risposta."

        with mock.patch.dict(os.environ, {"AGENT_ANSWER_MAX_RESULT_CHARS": "10"}):
            with mock.patch.object(answer.ollama_client, "chat_text", fake_chat_text):
                result = answer.compose_answer("le luci?", "devices", "x" * 50)
        self.assertEqual(result, "Risposta.")
        self.assertIn("x" * 10, captured["user_text"])
        self.assertNotIn("x" * 11, captured["user_text"])

    def test_none_on_llm_failure(self):
        with mock.patch.object(answer.ollama_client, "chat_text", return_value=None):
            self.assertIsNone(answer.compose_answer("le luci?", "devices", "dati"))


class ServiceAnswerTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"AGENT_ENABLED": "true"})
        self.env_patch.start()
        self.agent = AgentService(mock.Mock())

    def tearDown(self):
        self.env_patch.stop()

    def _propose(self, text, command="devices", readonly_result="2 dispositivi: lampada (on)"):
        with (
            mock.patch(
                "blackframe.agent.service.interpret",
                return_value=Suggestion(ok=True, command=command, arg=None),
            ),
            mock.patch(
                "blackframe.agent.service.registry_execute",
                return_value=CommandResult(text=readonly_result),
            ),
            mock.patch(
                "blackframe.agent.service.compose_answer",
                return_value="Sì, la lampada è accesa.",
            ) as compose,
        ):
            proposal = self.agent.propose(text, "web", "sess")
        return proposal, compose

    def test_question_on_readonly_gets_composed_answer(self):
        proposal, compose = self._propose("le luci sono accese?")
        compose.assert_called_once()
        self.assertEqual(proposal.answer, "Sì, la lampada è accesa.")
        self.assertEqual(proposal.result.text, "2 dispositivi: lampada (on)")

    def test_imperative_skips_composition(self):
        proposal, compose = self._propose("elenca i dispositivi")
        compose.assert_not_called()
        self.assertIsNone(proposal.answer)

    def test_inventory_question_uses_local_renderer(self):
        proposal, compose = self._propose(
            "che dispositivi hai?", command="inventory", readonly_result="Inventario sistema"
        )
        compose.assert_not_called()
        self.assertIsNone(proposal.answer)

    def test_entity_status_question_uses_local_renderer(self):
        proposal, compose = self._propose(
            "la lampada è accesa?",
            command="entity_status",
            readonly_result="Stato lampada: acceso",
        )
        compose.assert_not_called()
        self.assertIsNone(proposal.answer)

    def test_natural_answers_disabled_via_env(self):
        with mock.patch.dict(os.environ, {"AGENT_NATURAL_ANSWERS": "false"}):
            proposal, compose = self._propose("le luci sono accese?")
        compose.assert_not_called()
        self.assertIsNone(proposal.answer)

    def test_state_changing_command_never_composes(self):
        with (
            mock.patch(
                "blackframe.agent.service.interpret",
                return_value=Suggestion(ok=True, command="motion_off", arg=None),
            ),
            mock.patch("blackframe.agent.service.compose_answer") as compose,
        ):
            proposal = self.agent.propose("il movimento è attivo?", "web", "sess")
        compose.assert_not_called()
        self.assertIsNone(proposal.answer)
        self.assertIsNotNone(proposal.pending_id)


if __name__ == "__main__":
    unittest.main()
