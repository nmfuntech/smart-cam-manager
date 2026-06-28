"""Layer di automazione smart home disaccoppiato dalla pipeline video.

Fase 1: astrazione device (``SmartDevice``) + registry cifrato. Il rule engine e
il dispatcher arrivano nelle fasi successive. Tutto il pacchetto è inerte finché
non viene istanziato e agganciato esplicitamente dal core (``AUTOMATION_ENABLED``).
"""

from automation.devices import (
    DeviceError,
    MockDevice,
    SmartDevice,
    TuyaLanDevice,
    build_device,
)
from automation.engine import AutomationEngine, PlannedAction
from automation.events import CATEGORY_EVENT_MAP, EventContext
from automation.registry import DeviceRegistry
from automation.rules import Action, Rule, RuleConfigError, load_rules, parse_rules

__all__ = [
    "Action",
    "AutomationEngine",
    "CATEGORY_EVENT_MAP",
    "DeviceError",
    "DeviceRegistry",
    "EventContext",
    "MockDevice",
    "PlannedAction",
    "Rule",
    "RuleConfigError",
    "SmartDevice",
    "TuyaLanDevice",
    "build_device",
    "load_rules",
    "parse_rules",
]
