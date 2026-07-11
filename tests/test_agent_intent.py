import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from blackframe.agent import intent
from blackframe.agent.catalog import build_catalog_text, build_response_schema
from blackframe.agent.context import LastTurn
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

    def test_out_of_scope_is_rejected_without_ollama(self):
        with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
            result = intent.interpret("ordina una pizza margherita")
        chat_json.assert_not_called()
        self.assertFalse(result.ok)

    def test_destructive_request_is_rejected_even_with_domain_word(self):
        with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
            result = intent.interpret("cancella tutti i video registrati")
        chat_json.assert_not_called()
        self.assertFalse(result.ok)

    def test_oos_after_valid_context_is_still_rejected(self):
        turn = LastTurn(
            user_text="stato",
            command="status",
            arg=None,
            created_at=time.monotonic(),
        )
        with mock.patch.object(intent.ollama_client, "chat_json") as chat_json:
            result = intent.interpret("ordina una pizza", last_turn=turn)
        chat_json.assert_not_called()
        self.assertFalse(result.ok)

    def test_llm_result_cache_avoids_second_inference(self):
        with intent._CACHE_LOCK:
            intent._CACHE.clear()
        env = {"AGENT_FASTPATH": "false", "AGENT_CACHE": "true"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(
                intent.ollama_client,
                "chat_json",
                return_value={"command": "status", "arg": None},
            ) as chat_json:
                first = intent.interpret("controlla la telecamera")
                second = intent.interpret("controlla la telecamera")
        self.assertTrue(first.ok and second.ok)
        chat_json.assert_called_once()

    def test_concurrent_llm_request_fails_fast(self):
        self.assertTrue(intent._LLM_SLOT.acquire(blocking=False))
        try:
            with mock.patch.dict(
                os.environ,
                {"AGENT_FASTPATH": "false", "AGENT_CACHE": "false"},
            ):
                result = intent.interpret("controlla la telecamera")
        finally:
            intent._LLM_SLOT.release()
        self.assertFalse(result.ok)
        self.assertIn("occupato", result.reason)

    def test_ollama_unavailable_is_reported_as_not_ok(self):
        with mock.patch.object(intent.ollama_client, "chat_json", return_value=None):
            result = intent.interpret("controlla il movimento")
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

    def test_loosely_typed_device_arg_resolves_with_services(self):
        class FakeRegistry:
            def device_names(self):
                return ["lampada_ingresso"]

        services = mock.Mock(automation_registry=FakeRegistry())
        with mock.patch.object(
            intent.ollama_client,
            "chat_json",
            return_value={"command": "device_on", "arg": "la lampada dell'ingresso"},
        ):
            result = intent.interpret("accendi la lampada dell'ingresso", services=services)
        self.assertTrue(result.ok)
        self.assertEqual(result.arg, "lampada_ingresso")

    def test_prompt_sent_to_ollama_is_grounded_with_known_device_names(self):
        class FakeRegistry:
            def device_names(self):
                return ["lampada_ingresso"]

        services = mock.Mock(automation_registry=FakeRegistry())
        # Fast-path disattivato: qui si verifica il prompt inviato all'LLM,
        # e "accendi la lampada" verrebbe altrimenti risolto senza chiamarlo.
        with mock.patch.dict(os.environ, {"AGENT_FASTPATH": "false"}):
            with mock.patch.object(
                intent.ollama_client, "chat_json", return_value=None
            ) as chat_json:
                intent.interpret("accendi la lampada", services=services)
        system_prompt = chat_json.call_args[0][2]
        self.assertIn("lampada_ingresso", system_prompt)


class InterpretLlmWiringTests(unittest.TestCase):
    """Cosa arriva davvero a chat_json: schema, esempi, contesto."""

    def _interpret_and_capture(self, text, env=None, **kwargs):
        settings = {"AGENT_FASTPATH": "false", "AGENT_DOMAIN_GATE": "false", "AGENT_CACHE": "false"}
        settings.update(env or {})
        with mock.patch.dict(os.environ, settings):
            with mock.patch.object(
                intent.ollama_client, "chat_json", return_value=None
            ) as chat_json:
                intent.interpret(text, **kwargs)
        return chat_json.call_args

    def test_sentinel_command_is_rejected(self):
        with mock.patch.object(
            intent.ollama_client, "chat_json", return_value={"command": "nessuno", "arg": None}
        ):
            result = intent.interpret("che tempo fa domani?")
        self.assertFalse(result.ok)

    def test_response_schema_enum_contains_only_executable_commands(self):
        call = self._interpret_and_capture("frase qualunque")
        schema = call.kwargs["response_schema"]
        enum = schema["properties"]["command"]["anyOf"][0]["enum"]
        self.assertIn("status", enum)
        self.assertIn("nessuno", enum)
        self.assertNotIn("clip", enum)

    def test_response_schema_respects_exclude(self):
        call = self._interpret_and_capture("frase qualunque", exclude=WEB_EXCLUDED_COMMANDS)
        enum = call.kwargs["response_schema"]["properties"]["command"]["anyOf"][0]["enum"]
        self.assertNotIn("snapshot", enum)

    def test_schema_disabled_via_env(self):
        call = self._interpret_and_capture("frase qualunque", env={"AGENT_SCHEMA_FORMAT": "false"})
        self.assertIsNone(call.kwargs["response_schema"])

    def test_examples_prepended_as_chat_history(self):
        call = self._interpret_and_capture("frase qualunque")
        history = call.kwargs["history"]
        self.assertGreater(len(history), 0)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")

    def test_examples_disabled_via_env(self):
        call = self._interpret_and_capture(
            "frase qualunque", env={"AGENT_PROMPT_EXAMPLES": "false"}
        )
        self.assertEqual(call.kwargs["history"], [])

    def test_last_turn_appended_as_message_pair(self):
        turn = LastTurn(
            user_text="accendi la lampada dell'ingresso",
            command="device_on",
            arg="lampada_ingresso",
            created_at=time.monotonic(),
        )
        call = self._interpret_and_capture("ora spegnila", last_turn=turn)
        history = call.kwargs["history"]
        self.assertEqual(history[-2]["role"], "user")
        self.assertEqual(history[-2]["content"], "accendi la lampada dell'ingresso")
        self.assertEqual(
            json.loads(history[-1]["content"]),
            {"command": "device_on", "arg": "lampada_ingresso"},
        )

    def test_generation_options_passed(self):
        call = self._interpret_and_capture("frase qualunque", env={"AGENT_OLLAMA_NUM_CTX": "2048"})
        options = call.kwargs["options"]
        self.assertEqual(options["num_ctx"], 2048)
        self.assertEqual(options["num_predict"], 48)
        self.assertEqual(options["temperature"], 0.0)

    def test_high_confidence_family_reduces_schema(self):
        call = self._interpret_and_capture("controlla il movimento")
        enum = call.kwargs["response_schema"]["properties"]["command"]["anyOf"][0]["enum"]
        self.assertIn("motion_off", enum)
        self.assertNotIn("ptz_left", enum)

    def test_runtime_capabilities_remove_unavailable_tools(self):
        services = SimpleNamespace(
            features=SimpleNamespace(
                camera_profiles=SimpleNamespace(list_profiles=lambda: [])
            ),
            automation_registry=None,
        )

        excluded = intent._capability_exclude(services, frozenset())

        self.assertIn("device_on", excluded)
        self.assertIn("entity_status", excluded)
        self.assertIn("ptz_left", excluded)


class ResponseSchemaTests(unittest.TestCase):
    def test_schema_shape(self):
        schema = build_response_schema()
        self.assertEqual(schema["required"], ["command", "arg"])
        self.assertEqual(schema["properties"]["arg"]["type"], ["string", "null"])


class CatalogTests(unittest.TestCase):
    def test_catalog_excludes_non_executable_commands(self):
        text = build_catalog_text()
        self.assertNotIn("- clip:", text)
        self.assertIn("- status:", text)

    def test_catalog_respects_exclude_set(self):
        text = build_catalog_text(frozenset({"status"}))
        self.assertNotIn("- status:", text)
        self.assertIn("- config:", text)

    def test_catalog_without_known_names_uses_generic_placeholder(self):
        text = build_catalog_text()
        self.assertIn("- device_on:", text)
        self.assertIn("nome del dispositivo/regola", text)

    def test_catalog_with_known_names_grounds_device_arg_description(self):
        text = build_catalog_text(known_names={"device": ["lampada_ingresso", "presa_dj"]})
        device_line = next(line for line in text.splitlines() if line.startswith("- device_on:"))
        self.assertIn("lampada_ingresso", device_line)
        self.assertIn("presa_dj", device_line)


if __name__ == "__main__":
    unittest.main()
