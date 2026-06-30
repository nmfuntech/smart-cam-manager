import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from blackframe.automation import rules_store

_HAS_YAML = importlib.util.find_spec("yaml") is not None


@unittest.skipUnless(_HAS_YAML, "PyYAML non installato")
class RulesStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rules-store-"))
        self.path = self.tmpdir / "rules.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rule(self, name="r1", **over):
        base = {
            "name": name,
            "on": "person_detected",
            "do": [{"device": "luce", "action": "turn_on"}],
        }
        base.update(over)
        return base

    def test_missing_file_returns_empty(self):
        self.assertEqual(rules_store.load_rules_raw(self.path), [])

    def test_save_and_load_roundtrip(self):
        rules_store.save_rules_raw([self._rule()], self.path)
        loaded = rules_store.load_rules_raw(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "r1")
        # La chiave `on:` deve sopravvivere come stringa, non come True.
        self.assertEqual(loaded[0]["on"], "person_detected")

    def test_on_key_normalized_from_yaml_true(self):
        # PyYAML scrive `on:` non quotata e la rilegge come True: load_rules_raw
        # deve riportarla a "on".
        self.path.write_text(
            "- name: r1\n  on: person_detected\n  do:\n    - device: luce\n      action: turn_on\n"
        )
        loaded = rules_store.load_rules_raw(self.path)
        self.assertEqual(loaded[0].get("on"), "person_detected")
        self.assertNotIn(True, loaded[0])

    def test_upsert_replaces_same_name(self):
        rules_store.save_rules_raw([self._rule(name="r1", source="a")], self.path)
        rules_store.upsert_rule_raw(self._rule(name="r1", source="b"), self.path)
        loaded = rules_store.load_rules_raw(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["source"], "b")

    def test_upsert_appends_new_name(self):
        rules_store.upsert_rule_raw(self._rule(name="r1"), self.path)
        rules_store.upsert_rule_raw(self._rule(name="r2"), self.path)
        self.assertEqual({r["name"] for r in rules_store.load_rules_raw(self.path)}, {"r1", "r2"})

    def test_delete_existing(self):
        rules_store.save_rules_raw([self._rule(name="r1"), self._rule(name="r2")], self.path)
        self.assertTrue(rules_store.delete_rule_raw("r1", self.path))
        self.assertEqual([r["name"] for r in rules_store.load_rules_raw(self.path)], ["r2"])

    def test_delete_missing_returns_false(self):
        rules_store.save_rules_raw([self._rule(name="r1")], self.path)
        self.assertFalse(rules_store.delete_rule_raw("nope", self.path))

    def test_set_enabled_toggles(self):
        rules_store.save_rules_raw([self._rule(name="r1")], self.path)
        self.assertTrue(rules_store.set_rule_enabled("r1", False, self.path))
        self.assertFalse(rules_store.load_rules_raw(self.path)[0]["enabled"])
        self.assertTrue(rules_store.set_rule_enabled("r1", True, self.path))
        self.assertTrue(rules_store.load_rules_raw(self.path)[0]["enabled"])

    def test_set_enabled_missing_returns_false(self):
        rules_store.save_rules_raw([self._rule(name="r1")], self.path)
        self.assertFalse(rules_store.set_rule_enabled("nope", False, self.path))


if __name__ == "__main__":
    unittest.main()
