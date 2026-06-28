"""Rule engine: da un evento cam alle azioni device, secondo le regole.

L'engine è puro e sincrono: prende un EventContext, trova le regole che matchano
(evento, sorgente, finestra oraria, cooldown) e restituisce le azioni pianificate.
Non parla coi device: passa le azioni a un dispatcher (Fase 3) che le esegue su un
thread separato con retry/isolamento. Questa separazione tiene l'engine testabile
senza hardware e garantisce che il match non blocchi mai il thread di video-analisi.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from automation.events import EventContext
from automation.rules import Action, Rule, minute_in_window

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedAction:
    """Azione decisa dall'engine, pronta per il dispatcher."""

    rule_name: str
    action: Action


class AutomationEngine:
    """Matcha gli eventi contro le regole ed emette le azioni risultanti.

    Args:
        rules: regole già validate (vedi automation.rules.load_rules).
        dispatcher: oggetto con `submit(PlannedAction)`; se None, emit() si limita
            a restituire le azioni (utile nei test e finché il dispatcher non esiste).
        monotonic: sorgente di tempo monotòno per il cooldown (iniettabile nei test).
    """

    def __init__(self, rules, dispatcher=None, monotonic=time.monotonic) -> None:
        self.rules: tuple[Rule, ...] = tuple(rules)
        self._dispatcher = dispatcher
        self._monotonic = monotonic
        # Ultimo istante (monotòno) in cui ciascuna regola ha fatto fuoco.
        self._last_fired: dict[str, float] = {}

    def emit(self, ctx: EventContext) -> list[PlannedAction]:
        """Valuta un evento e restituisce (ed eventualmente dispatcha) le azioni."""
        planned: list[PlannedAction] = []
        now = self._monotonic()
        for rule in self.rules:
            if not self._matches(rule, ctx):
                continue
            if self._in_cooldown(rule, now):
                logger.debug("Regola '%s' in cooldown, evento ignorato", rule.name)
                continue
            self._last_fired[rule.name] = now
            for action in rule.actions:
                planned.append(PlannedAction(rule_name=rule.name, action=action))

        if self._dispatcher is not None:
            for item in planned:
                # Il dispatcher isola i propri errori; qui restiamo difensivi così
                # un suo malfunzionamento non si propaga verso chi ci ha chiamato.
                try:
                    self._dispatcher.submit(item)
                except Exception:
                    logger.exception("Submit azione al dispatcher fallito (%s)", item.rule_name)
        return planned

    # --- matching -----------------------------------------------------------

    def _matches(self, rule: Rule, ctx: EventContext) -> bool:
        if rule.event != ctx.event_name:
            return False
        if rule.source is not None and rule.source != ctx.source:
            return False
        if rule.window is not None and not self._in_window(rule, ctx):
            return False
        return True

    @staticmethod
    def _in_window(rule: Rule, ctx: EventContext) -> bool:
        # Usa l'orario dell'evento se disponibile, altrimenti l'ora corrente.
        epoch = ctx.timestamp if ctx.timestamp is not None else time.time()
        local = datetime.fromtimestamp(epoch)
        minute = local.hour * 60 + local.minute
        return minute_in_window(minute, rule.window)

    def _in_cooldown(self, rule: Rule, now: float) -> bool:
        if rule.cooldown_seconds <= 0:
            return False
        last = self._last_fired.get(rule.name)
        return last is not None and (now - last) < rule.cooldown_seconds
