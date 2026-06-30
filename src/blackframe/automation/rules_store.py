"""Lettura/scrittura grezza del file regole YAML.

Le regole vivono in ``config/automation/rules.yaml``. Questo modulo è il punto
unico per leggerle/scriverle come dizionari grezzi (prima della validazione di
``rules.parse_rules``), così che sia il web layer (``routes/automation.py``) sia
il bot Telegram (``telegram_commands.py``) condividano la stessa logica di I/O,
compresa la normalizzazione della chiave ``on:`` che PyYAML 1.1 interpreta come
``True``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def rules_path() -> str:
    return os.getenv("AUTOMATION_RULES_PATH", "config/automation/rules.yaml")


def load_rules_raw(path: str | Path | None = None) -> list[dict]:
    """Carica le regole come lista di dict grezzi. File assente / illeggibile = []."""
    rules_file = Path(path or rules_path())
    if not rules_file.exists():
        return []
    try:
        import yaml  # noqa: PLC0415 — dep opzionale a runtime

        data = yaml.safe_load(rules_file.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            return []
        # PyYAML 1.1 interpreta la chiave non quotata `on:` come Python True:
        # la riportiamo a "on" così il resto del codice vede una forma coerente.
        normalized = []
        for rule in data:
            if isinstance(rule, dict) and True in rule and "on" not in rule:
                rule = {("on" if k is True else k): v for k, v in rule.items()}
            normalized.append(rule)
        return normalized
    except Exception:
        logger.exception("Lettura rules.yaml fallita")
        return []


def save_rules_raw(rules: list[dict], path: str | Path | None = None) -> None:
    """Scrive la lista di regole su YAML, preservando l'ordine delle chiavi."""
    import yaml  # noqa: PLC0415

    rules_file = Path(path or rules_path())
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(
        rules or [],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    rules_file.write_text(content, encoding="utf-8")


def upsert_rule_raw(rule_dict: dict, path: str | Path | None = None) -> None:
    """Inserisce o sostituisce (per ``name``) una regola e riscrive il file."""
    name = str(rule_dict.get("name") or "").strip()
    existing = [r for r in load_rules_raw(path) if r.get("name") != name]
    existing.append(rule_dict)
    save_rules_raw(existing, path)


def delete_rule_raw(name: str, path: str | Path | None = None) -> bool:
    """Rimuove la regola con quel nome. False se non esisteva."""
    existing = load_rules_raw(path)
    updated = [r for r in existing if r.get("name") != name]
    if len(updated) == len(existing):
        return False
    save_rules_raw(updated, path)
    return True


def set_rule_enabled(name: str, enabled: bool, path: str | Path | None = None) -> bool:
    """Imposta il flag ``enabled`` di una regola. False se la regola non esiste."""
    rules = load_rules_raw(path)
    found = False
    for rule in rules:
        if isinstance(rule, dict) and rule.get("name") == name:
            rule["enabled"] = bool(enabled)
            found = True
            break
    if not found:
        return False
    save_rules_raw(rules, path)
    return True
