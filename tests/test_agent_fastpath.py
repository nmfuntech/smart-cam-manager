import os
import unittest
from unittest import mock

from blackframe.agent import fastpath, intent


class FakeRegistry:
    def __init__(self, names):
        self._names = names

    def device_names(self):
        return list(self._names)


class FakeServices:
    def __init__(self, names):
        self.automation_registry = FakeRegistry(names)


class MatchTests(unittest.TestCase):
    def test_keyword_whole_message_matches(self):
        self.assertEqual(fastpath.match("stato"), {"command": "status", "arg": None})
        self.assertEqual(fastpath.match("Eventi"), {"command": "events", "arg": None})
        self.assertEqual(fastpath.match("come va?"), {"command": "status", "arg": None})

    def test_keyword_does_not_match_substrings(self):
        # "stato" dentro una frase piu' lunga non deve scattare: decide l'LLM.
        self.assertIsNone(fastpath.match("stato della lampada"))
        self.assertIsNone(fastpath.match("dammi lo stato e una foto"))

    def test_device_on_resolves_unique_name(self):
        services = FakeServices(["lampada_ingresso", "presa_salotto"])
        result = fastpath.match("accendi la lampada dell'ingresso", services=services)
        self.assertEqual(result, {"command": "device_on", "arg": "lampada_ingresso"})

    def test_device_off_resolves_unique_name(self):
        services = FakeServices(["lampada_ingresso", "presa_salotto"])
        result = fastpath.match("spegni la presa del salotto", services=services)
        self.assertEqual(result, {"command": "device_off", "arg": "presa_salotto"})

    def test_ambiguous_device_falls_through(self):
        services = FakeServices(["lampada_ingresso", "lampada_salotto"])
        self.assertIsNone(fastpath.match("accendi la lampada", services=services))

    def test_toggle_phrases_fall_through_to_llm(self):
        # "attiva il rilevamento movimento" ha il prefisso device ma non
        # risolve a nessun device: deve decidere l'LLM (motion_on).
        services = FakeServices(["lampada_ingresso"])
        self.assertIsNone(fastpath.match("attiva il rilevamento movimento", services=services))

    def test_ptz_verb_plus_single_direction(self):
        self.assertEqual(
            fastpath.match("gira la camera a sinistra"), {"command": "ptz_left", "arg": None}
        )
        self.assertEqual(fastpath.match("muovi a destra"), {"command": "ptz_right", "arg": None})

    def test_ptz_requires_verb_and_unambiguous_direction(self):
        self.assertIsNone(fastpath.match("destra"))
        self.assertIsNone(fastpath.match("sposta a sinistra e poi a destra"))

    def test_exclude_is_respected(self):
        self.assertIsNone(fastpath.match("foto", exclude=frozenset({"snapshot"})))


class InterpretIntegrationTests(unittest.TestCase):
    def test_fastpath_hit_never_calls_ollama(self):
        with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
            result = intent.interpret("stato")
        chat_json.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.command, "status")

    def test_fastpath_mutation_still_validated_and_proposed(self):
        services = mock.Mock(automation_registry=FakeRegistry(["lampada_ingresso"]))
        with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
            result = intent.interpret("accendi la lampada dell'ingresso", services=services)
        chat_json.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.command, "device_on")
        self.assertEqual(result.arg, "lampada_ingresso")

    def test_fastpath_disabled_via_env(self):
        with mock.patch.dict(os.environ, {"AGENT_FASTPATH": "false"}):
            with mock.patch.object(
                intent.ollama_client, "chat_json", return_value=None
            ) as chat_json:
                result = intent.interpret("stato")
        chat_json.assert_called_once()
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
