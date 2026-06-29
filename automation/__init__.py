"""Layer di automazione smart home disaccoppiato dalla pipeline video.

Tutto il pacchetto è inerte finché non viene istanziato e agganciato
esplicitamente dal core (``AUTOMATION_ENABLED``).
"""

from automation.devices import (
    DeviceError,
    MockDevice,
    SmartDevice,
    TuyaLanDevice,
    build_device,
)
from automation.dispatcher import ActionDispatcher
from automation.engine import AutomationEngine, PlannedAction
from automation.events import CATEGORY_EVENT_MAP, EventContext
from automation.registry import DeviceRegistry
from automation.rules import Action, Rule, RuleConfigError, load_rules, parse_rules

__all__ = [
    "Action",
    "ActionDispatcher",
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
