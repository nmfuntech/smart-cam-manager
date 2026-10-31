import os
import unittest
import zlib
from unittest import mock

from blackframe.agent import intent, semantic
from blackframe.commands.naming import normalize_identifier

DIM = 512


def fake_embed(base_url, model, texts, *, timeout=10.0, keep_alive=None):
    """Bag-of-words vectors: phrases sharing words are similar."""
    vectors = []
    for text in texts:
        vector = [0.0] * DIM
        for token in normalize_identifier(text).split("_"):
            if token:
                vector[zlib.crc32(token.encode()) % DIM] += 1.0
        vectors.append(vector)
    return vectors


class SemanticTestCase(unittest.TestCase):
    def setUp(self):
        env = {
            "AGENT_EMBED_MODEL": "fake-embed",
            "AGENT_SEMANTIC_MIN_SCORE": "0.7",
            "AGENT_SEMANTIC_MARGIN": "0.05",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        embed = mock.patch.object(semantic.ollama_client, "embed", side_effect=fake_embed)
        self.embed = embed.start()
        self.addCleanup(embed.stop)
        self.classifier = semantic.SemanticClassifier()
        self.assertTrue(self.classifier.build())
        self.all_commands = {name for name in semantic._load_examples() if name != "nessuno"}


class ClassifierTests(SemanticTestCase):
    def test_confident_match(self):
        match = self.classifier.classify("disattiva le notifiche", self.all_commands, {})
        self.assertEqual(match.command, "notifications_off")

    def test_out_of_scope_class(self):
        match = self.classifier.classify("ordina una pizza", self.all_commands, {})
        self.assertEqual(match.command, semantic.NO_COMMAND)

    def test_low_score_defers_to_llm(self):
        self.assertIsNone(self.classifier.classify("xyzzy plugh", self.all_commands, {}))

    def test_name_argument_is_resolved_or_deferred(self):
        names = {"device": ["lampada_ingresso", "presa_salotto"]}
        match = self.classifier.classify("spegni la presa del salotto", self.all_commands, names)
        self.assertEqual((match.command, match.arg), ("device_off", "presa_salotto"))
        self.assertIsNone(self.classifier.classify("spegni la presa", self.all_commands, {}))

    def test_disallowed_commands_are_never_returned(self):
        allowed = self.all_commands - {"notifications_off"}
        match = self.classifier.classify("disattiva le notifiche", allowed, {})
        self.assertTrue(match is None or match.command != "notifications_off")

    def test_example_vectors_are_cached_on_disk(self):
        calls = self.embed.call_count
        other = semantic.SemanticClassifier()
        self.assertTrue(other.build())
        self.assertEqual(self.embed.call_count, calls)


class ArgParserTests(unittest.TestCase):
    def test_minutes(self):
        cases = {"per_2_ore": "120", "mezz_ora": "30", "un_ora": "60", "10_minuti": "10"}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(semantic.parse_minutes(text), expected)
        self.assertIsNone(semantic.parse_minutes("un_attimo"))

    def test_sensitivity(self):
        self.assertEqual(semantic.parse_sensitivity("sensibilita_al_massimo"), "alta")
        self.assertEqual(semantic.parse_sensitivity("troppo_sensibile_abbassala"), "bassa")
        self.assertIsNone(semantic.parse_sensitivity("cambia_la_sensibilita"))

    def test_ambiguous_name_is_none(self):
        names = ["luce_cucina", "luce_bagno"]
        self.assertIsNone(semantic.resolve_mentioned_name("accendi_la_luce", names))
        self.assertEqual(semantic.resolve_mentioned_name("luce_bagno_on", names), "luce_bagno")


class InterpretIntegrationTests(SemanticTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(semantic, "_classifier", self.classifier)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _interpret(self, text):
        env = {"AGENT_FASTPATH": "false", "AGENT_CACHE": "false"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(intent.ollama_client, "chat_json") as chat:
                chat.return_value = {"command": "nessuno", "arg": None}
                return intent.interpret(text), chat

    def test_semantic_answer_skips_llm(self):
        result, chat = self._interpret("disattiva le notifiche")
        self.assertEqual((result.command, result.source), ("notifications_off", "semantic"))
        chat.assert_not_called()

    def test_semantic_out_of_scope_rejects_without_llm(self):
        result, chat = self._interpret("ordina una pizza")
        self.assertFalse(result.ok)
        chat.assert_not_called()

    def test_destructive_message_never_uses_semantic(self):
        result, _ = self._interpret("cancella le notifiche")
        self.assertNotEqual(result.source, "semantic")

    def test_info_question_cannot_become_an_action(self):
        result, _ = self._interpret("il cane e' uscito?")
        self.assertTrue(result.command is None or result.command not in intent._MUTATING_COMMANDS)


class TopicResolverTests(unittest.TestCase):
    def test_polarity_and_negation(self):
        self.assertEqual(semantic.resolve_in_topic("motion", "basta rilevamento"), "motion_off")
        self.assertEqual(
            semantic.resolve_in_topic("motion", "riattiva il rilevamento"), "motion_on"
        )
        self.assertEqual(
            semantic.resolve_in_topic("notifications", "non mandarmi notifiche"),
            "notifications_off",
        )
        self.assertIsNone(semantic.resolve_in_topic("motion", "il rilevamento"))

    def test_ptz_and_modes(self):
        self.assertEqual(semantic.resolve_in_topic("ptz", "un po' piu a sx"), "ptz_left")
        self.assertIsNone(semantic.resolve_in_topic("ptz", "destra o sinistra"))
        self.assertEqual(semantic.resolve_in_topic("mode_set", "arma la casa"), "arma")
        self.assertEqual(semantic.resolve_in_topic("mode_set", "esco di casa"), "arma")
        self.assertEqual(semantic.resolve_in_topic("mode_set", "sono rientrato"), "casa")
        self.assertEqual(semantic.resolve_in_topic("mode_set", "spegni l'allarme"), "disarma")

    def test_shortlist_covers_top_topics_only(self):
        scores = {"motion_on": 0.75, "motion_off": 0.5, "ptz_left": 0.72, "events": 0.71,
                  "status": 0.4, "nessuno": 0.3}  # fmt: skip
        allowed = {"motion_on", "motion_off", "ptz_left", "ptz_right", "events", "status"}
        short = semantic.shortlist(scores, allowed, topics=2, min_score=0.7)
        self.assertEqual(short, {"motion_on", "motion_off", "ptz_left", "ptz_right"})
        self.assertIsNone(semantic.shortlist({**scores, "nessuno": 0.9}, allowed))

    def test_out_of_scope_close_to_top_keeps_only_readonly(self):
        scores = {"disarma": 0.83, "ptz_home": 0.70, "events": 0.79, "nessuno": 0.81}
        allowed = {"disarma", "arma", "casa", "ptz_home", "ptz_left", "events"}
        short = semantic.shortlist(scores, allowed, none_margin=0.03)
        self.assertEqual(short, {"events"})
        # Far from "nessuno": state-changing commands stay available.
        far = semantic.shortlist({**scores, "nessuno": 0.6}, allowed, none_margin=0.03)
        self.assertIn("disarma", far)
        only_mutating = {"disarma", "arma", "casa", "ptz_home", "ptz_left"}
        self.assertEqual(semantic.shortlist(scores, only_mutating, none_margin=0.03), frozenset())


class LlmCorrectionTests(unittest.TestCase):
    def test_llm_polarity_mistake_is_fixed(self):
        fixed = intent._correct_variant({"command": "motion_on", "arg": None}, "basta rilevamento",
                                        frozenset())  # fmt: skip
        self.assertEqual(fixed["command"], "motion_off")

    def test_device_argument_is_kept(self):
        response = {"command": "device_on", "arg": "presa_salotto"}
        fixed = intent._correct_variant(response, "spegni la presa", frozenset())
        self.assertEqual(fixed, {"command": "device_off", "arg": "presa_salotto"})

    def test_correction_never_picks_an_excluded_command(self):
        response = {"command": "motion_on", "arg": None}
        fixed = intent._correct_variant(response, "basta", frozenset({"motion_off"}))
        self.assertEqual(fixed["command"], "motion_on")


class ShortlistIntegrationTests(SemanticTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(semantic, "_classifier", self.classifier)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_abstention_narrows_llm_schema_and_skips_domain_gate(self):
        env = {
            "AGENT_FASTPATH": "false",
            "AGENT_CACHE": "false",
            "AGENT_SEMANTIC_MIN_SCORE": "0.99",
            "AGENT_SEMANTIC_SHORTLIST_SCORE": "0.3",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(intent.ollama_client, "chat_json") as chat:
                chat.return_value = {"command": "nessuno", "arg": None}
                intent.interpret("le notifiche basta")
        chat.assert_called_once()
        enum = chat.call_args.kwargs["response_schema"]["properties"]["command"]["anyOf"][0]
        self.assertIn("notifications_off", enum["enum"])
        self.assertLess(len(enum["enum"]), 15)

    def test_empty_shortlist_rejects_without_llm(self):
        empty = semantic.SemanticResult(None, frozenset())
        with mock.patch.dict(os.environ, {"AGENT_FASTPATH": "false", "AGENT_CACHE": "false"}):
            with mock.patch.object(self.classifier, "analyze", return_value=empty):
                with mock.patch.object(intent.ollama_client, "chat_json") as chat:
                    result = intent.interpret("resetta tutto il sistema")
        chat.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.source, "semantic")
