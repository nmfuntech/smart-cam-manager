import os
import unittest
from unittest import mock

from blackframe.agent import intent
from blackframe.agent.catalog import build_catalog_text
from blackframe.agent.service import WEB_EXCLUDED_COMMANDS


class InterpretTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"AGENT_MAX_INPUT_CHARS": "300"})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_empty_text_is_rejected_without_calling_ollama(self):
        with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
            result = intent.interpret("   ")
        chat_json.assert_not_called()
        self.assertFalse(result.ok)

    def test_overlong_text_is_rejected_without_calling_ollama(self):
        with mock.patch.dict(os.environ, {"AGENT_MAX_INPUT_CHARS": "5"}):
            with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
                result = intent.interpret("questo testo e' troppo lungo")
        chat_json.assert_not_called()
        self.assertFalse(result.ok)

    def test_ollama_unavailable_is_reported_as_not_ok(self):
        with mock.patch.object(intent.ollama_client, "chat_json", return_value=None):
            result = intent.interpret("spegni il movimento")
        self.assertFalse(result.ok)
        self.assertIn("non disponibile", result.reason.lower())

    def test_valid_command_without_arg_is_accepted(self):
        with mock.patch.object(
            intent.ollama_client, "chat_json", return_value={"command": "status", "arg": None}
        ):
            result = intent.interpret("come sta la telecamera?")
        self.assertTrue(result.ok)
        self.assertEqual(result.command, "status")
        self.assertIsNone(result.arg)

    def test_hallucinated_command_is_rejected(self):
        with mock.patch.object(
            intent.ollama_client,
            "chat_json",
            return_value={"command": "delete_all_footage", "arg": None},
        ):
            result = intent.interpret("cancella tutto")
        self.assertFalse(result.ok)

    def test_non_executable_catalog_entry_is_rejected(self):
        # "clip" e' nel COMMAND_REGISTRY ma senza handler (catalogo-only):
        # anche se il modello lo propone, non deve mai passare la validazione.
        with mock.patch.object(
            intent.ollama_client, "chat_json", return_value={"command": "clip", "arg": "10"}
        ):
            result = intent.interpret("fammi una clip")
        self.assertFalse(result.ok)

    def test_invalid_arg_is_rejected(self):
        with mock.patch.object(
            intent.ollama_client,
            "chat_json",
            return_value={"command": "sensitivity", "arg": "altissima"},
        ):
            result = intent.interpret("metti la sensibilita' altissima")
        self.assertFalse(result.ok)

    def test_valid_enum_arg_is_normalized(self):
        with mock.patch.object(
            intent.ollama_client,
            "chat_json",
            return_value={"command": "sensitivity", "arg": "ALTA"},
        ):
            result = intent.interpret("aumenta la sensibilita'")
        self.assertTrue(result.ok)
        self.assertEqual(result.arg, "alta")

    def test_excluded_command_is_rejected_even_if_model_proposes_it(self):
        with mock.patch.object(
            intent.ollama_client, "chat_json", return_value={"command": "snapshot", "arg": None}
        ):
            result = intent.interpret("mandami una foto", exclude=WEB_EXCLUDED_COMMANDS)
        self.assertFalse(result.ok)


class CatalogTests(unittest.TestCase):
    def test_catalog_excludes_non_executable_commands(self):
        text = build_catalog_text()
        self.assertNotIn("- clip:", text)
        self.assertIn("- status:", text)

    def test_catalog_respects_exclude_set(self):
        text = build_catalog_text(frozenset({"status"}))
        self.assertNotIn("- status:", text)
        self.assertIn("- config:", text)


if __name__ == "__main__":
    unittest.main()
