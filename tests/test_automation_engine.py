import importlib.util
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from automation.engine import AutomationEngine
from automation.events import EventContext
from automation.rules import (
    RuleConfigError,
    load_rules,
    minute_in_window,
    parse_duration,
    parse_rules,
    parse_time_window,
)

_HAS_YAML = importlib.util.find_spec("yaml") is not None


class DurationParsingTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("120s"), 120.0)
        self.assertEqual(parse_duration("5m"), 300.0)
        self.assertEqual(parse_duration("1h"), 3600.0)
        self.assertEqual(parse_duration("90"), 90.0)
        self.assertEqual(parse_duration(45), 45.0)
        self.assertEqual(parse_duration(None), 0.0)

    def test_invalid(self):
        with self.assertRaises(RuleConfigError):
            parse_duration("soon")


class TimeWindowTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_time_window(["18:00", "07:00"]), (1080, 420))
        self.assertIsNone(parse_time_window(None))

    def test_parse_invalid(self):
        with self.assertRaises(RuleConfigError):
            parse_time_window(["25:00", "07:00"])
        with self.assertRaises(RuleConfigError):
            parse_time_window(["18:00"])

    def test_same_day_window(self):
        window = (9 * 60, 17 * 60)  # 09:00-17:00
        self.assertTrue(minute_in_window(12 * 60, window))
        self.assertFalse(minute_in_window(8 * 60, window))
        self.assertFalse(minute_in_window(17 * 60, window))  # end esclusivo

    def test_overnight_window(self):
        window = (18 * 60, 7 * 60)  # 18:00-07:00
        self.assertTrue(minute_in_window(23 * 60, window))  # 23:00
        self.assertTrue(minute_in_window(3 * 60, window))  # 03:00
        self.assertFalse(minute_in_window(12 * 60, window))  # 12:00

    def test_full_day_when_equal(self):
        self.assertTrue(minute_in_window(0, (0, 0)))
        self.assertTrue(minute_in_window(720, (0, 0)))


class ParseRulesTests(unittest.TestCase):
    def _rule(self, **over):
        base = {
            "name": "r1",
            "on": "person_detected",
            "do": [{"device": "luce", "action": "turn_on"}],
        }
        base.update(over)
        return base

    def test_minimal_valid(self):
        rules = parse_rules([self._rule()])
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule.name, "r1")
        self.assertEqual(rule.event, "person_detected")
        self.assertEqual(rule.actions[0].device, "luce")
        self.assertIsNone(rule.source)
        self.assertIsNone(rule.window)
        self.assertEqual(rule.cooldown_seconds, 0.0)

    def test_full_rule(self):
        rule = parse_rules(
            [
                self._rule(
                    source="ingresso",
                    between=["18:00", "07:00"],
                    cooldown="120s",
                    do=[
                        {"device": "luce", "action": "turn_on", "for": "5m"},
                        {"device": "presa", "action": "set_state", "state": {"bright": 80}},
                    ],
                )
            ]
        )[0]
        self.assertEqual(rule.source, "ingresso")
        self.assertEqual(rule.window, (1080, 420))
        self.assertEqual(rule.cooldown_seconds, 120.0)
        self.assertEqual(rule.actions[0].for_seconds, 300.0)
        self.assertEqual(rule.actions[1].state, {"bright": 80})
        self.assertEqual(rule.devices, frozenset({"luce", "presa"}))

    def test_none_returns_empty(self):
        self.assertEqual(parse_rules(None), [])

    def test_not_a_list(self):
        with self.assertRaises(RuleConfigError):
            parse_rules({"name": "x"})

    def test_missing_name(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(name="")])

    def test_bad_event(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(on="explosion_detected")])

    def test_empty_do(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(do=[])])

    def test_bad_action(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(do=[{"device": "luce", "action": "explode"}])])

    def test_set_state_requires_state(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(do=[{"device": "luce", "action": "set_state"}])])

    def test_state_only_with_set_state(self):
        with self.assertRaises(RuleConfigError):
            parse_rules(
                [self._rule(do=[{"device": "luce", "action": "turn_on", "state": {"x": 1}}])]
            )

    def test_action_missing_device(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(do=[{"action": "turn_on"}])])

    def test_duplicate_names(self):
        with self.assertRaises(RuleConfigError):
            parse_rules([self._rule(name="dup"), self._rule(name="dup")])

    def test_unknown_device_rejected(self):
        with self.assertRaises(RuleConfigError):
            parse_rules(
                [self._rule(do=[{"device": "ignota", "action": "turn_on"}])], known_devices={"luce"}
            )

    def test_known_device_accepted(self):
        rules = parse_rules([self._rule()], known_devices={"luce", "presa"})
        self.assertEqual(len(rules), 1)


class _FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class EngineMatchTests(unittest.TestCase):
    def _engine(self, raw_rules, clock=None):
        rules = parse_rules(raw_rules)
        return AutomationEngine(rules, monotonic=clock or _FakeClock())

    def _person_rule(self, **over):
        base = {
            "name": "luce_persona",
            "on": "person_detected",
            "do": [{"device": "luce", "action": "turn_on"}],
        }
        base.update(over)
        return base

    def test_matches_event(self):
        engine = self._engine([self._person_rule()])
        planned = engine.emit(EventContext("e1", "persona"))
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].rule_name, "luce_persona")
        self.assertEqual(planned[0].action.device, "luce")

    def test_no_match_on_other_event(self):
        engine = self._engine([self._person_rule()])
        self.assertEqual(engine.emit(EventContext("e1", "animale_domestico")), [])

    def test_source_filter(self):
        engine = self._engine([self._person_rule(source="ingresso")])
        self.assertEqual(engine.emit(EventContext("e", "persona", source="garage")), [])
        self.assertEqual(len(engine.emit(EventContext("e", "persona", source="ingresso"))), 1)

    def test_multiple_actions_and_rules(self):
        engine = self._engine(
            [
                self._person_rule(
                    do=[
                        {"device": "luce", "action": "turn_on"},
                        {"device": "presa", "action": "turn_on"},
                    ]
                ),
                {
                    "name": "altra",
                    "on": "person_detected",
                    "do": [{"device": "sirena", "action": "turn_on"}],
                },
            ]
        )
        planned = engine.emit(EventContext("e", "persona"))
        self.assertEqual(sorted(p.action.device for p in planned), ["luce", "presa", "sirena"])

    def test_time_window_blocks_outside(self):
        engine = self._engine([self._person_rule(between=["18:00", "07:00"])])
        # 12:00 = fuori finestra notturna
        noon = datetime(2026, 6, 28, 12, 0).timestamp()
        self.assertEqual(engine.emit(EventContext("e", "persona", timestamp=noon)), [])
        # 23:00 = dentro
        night = datetime(2026, 6, 28, 23, 0).timestamp()
        self.assertEqual(len(engine.emit(EventContext("e", "persona", timestamp=night))), 1)

    def test_cooldown_suppresses_then_recovers(self):
        clock = _FakeClock()
        engine = self._engine([self._person_rule(cooldown="120s")], clock=clock)
        self.assertEqual(len(engine.emit(EventContext("e", "persona"))), 1)
        clock.advance(60)
        self.assertEqual(engine.emit(EventContext("e", "persona")), [])  # ancora in cooldown
        clock.advance(61)  # totale 121s > 120s
        self.assertEqual(len(engine.emit(EventContext("e", "persona"))), 1)

    def test_no_cooldown_fires_every_time(self):
        engine = self._engine([self._person_rule()])
        self.assertEqual(len(engine.emit(EventContext("e", "persona"))), 1)
        self.assertEqual(len(engine.emit(EventContext("e", "persona"))), 1)


class _RecordingDispatcher:
    def __init__(self, fail=False):
        self.submitted = []
        self.fail = fail

    def submit(self, planned):
        if self.fail:
            raise RuntimeError("dispatcher giù")
        self.submitted.append(planned)


class EngineDispatcherTests(unittest.TestCase):
    def test_submits_to_dispatcher(self):
        rules = parse_rules(
            [
                {
                    "name": "r",
                    "on": "person_detected",
                    "do": [{"device": "luce", "action": "turn_on"}],
                }
            ]
        )
        dispatcher = _RecordingDispatcher()
        engine = AutomationEngine(rules, dispatcher=dispatcher, monotonic=_FakeClock())
        engine.emit(EventContext("e", "persona"))
        self.assertEqual(len(dispatcher.submitted), 1)

    def test_dispatcher_error_does_not_propagate(self):
        rules = parse_rules(
            [
                {
                    "name": "r",
                    "on": "person_detected",
                    "do": [{"device": "luce", "action": "turn_on"}],
                }
            ]
        )
        engine = AutomationEngine(
            rules, dispatcher=_RecordingDispatcher(fail=True), monotonic=_FakeClock()
        )
        # Un dispatcher che esplode non deve mai propagare verso il chiamante
        # (che gira vicino al thread di video-analisi).
        planned = engine.emit(EventContext("e", "persona"))
        self.assertEqual(len(planned), 1)


@unittest.skipUnless(_HAS_YAML, "PyYAML non installato")
class LoadRulesYamlTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rules-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_valid_yaml(self):
        path = self.tmpdir / "rules.yaml"
        path.write_text(
            "- name: r1\n  on: person_detected\n  do:\n    - device: luce\n      action: turn_on\n"
        )
        rules = load_rules(path)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].name, "r1")

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_rules(self.tmpdir / "nope.yaml"), [])

    def test_example_file_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "automation" / "rules.example.yaml"
        rules = load_rules(example)
        self.assertTrue(any(r.name == "luce_ingresso_notturna" for r in rules))


if __name__ == "__main__":
    unittest.main()
