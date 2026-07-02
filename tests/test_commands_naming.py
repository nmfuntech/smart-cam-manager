import unittest

from blackframe.commands.naming import normalize_identifier, resolve_name


class NormalizeIdentifierTests(unittest.TestCase):
    def test_lowercases_strips_accents_and_replaces_separators(self):
        self.assertEqual(normalize_identifier("Lampada Ingresso"), "lampada_ingresso")
        self.assertEqual(normalize_identifier("l'ingresso"), "l_ingresso")
        self.assertEqual(normalize_identifier("Caffè"), "caffe")

    def test_empty_input_normalizes_to_empty_string(self):
        self.assertEqual(normalize_identifier("   "), "")
        self.assertEqual(normalize_identifier(None), "")


class ResolveNameTests(unittest.TestCase):
    def setUp(self):
        self.candidates = ["lampada_ingresso", "luce_scrivania_1", "luce_scrivania_2", "presa_dj"]

    def test_exact_match(self):
        name, suggestions = resolve_name("lampada_ingresso", self.candidates)
        self.assertEqual(name, "lampada_ingresso")
        self.assertEqual(suggestions, [])

    def test_token_match_ignoring_italian_prepositions(self):
        raw = normalize_identifier("la lampada dell'ingresso")
        name, suggestions = resolve_name(raw, self.candidates)
        self.assertEqual(name, "lampada_ingresso")

    def test_ambiguous_token_match_returns_no_resolution(self):
        raw = normalize_identifier("luce scrivania")
        name, suggestions = resolve_name(raw, self.candidates)
        self.assertIsNone(name)
        self.assertIn("luce_scrivania_1", suggestions)
        self.assertIn("luce_scrivania_2", suggestions)

    def test_typo_resolves_via_close_match(self):
        name, suggestions = resolve_name("presa_dg", self.candidates)
        self.assertEqual(name, "presa_dj")

    def test_no_match_returns_suggestions_or_empty(self):
        name, suggestions = resolve_name("frigorifero", self.candidates)
        self.assertIsNone(name)

    def test_no_candidates_returns_none(self):
        self.assertEqual(resolve_name("lampada_ingresso", []), (None, []))
