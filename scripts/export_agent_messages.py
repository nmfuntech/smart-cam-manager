#!/usr/bin/env python3
"""Turn the agent message log into a draft evaluation set.

Reads ``data/agent_messages.jsonl`` (written by the running app) and writes a
JSON file in the same format as ``scripts/agent_eval_cases.json``. Each unique
message gets the command the agent proposed as ``expect_command`` and a tag
telling how much to trust it:

- ``confirmed``: the user pressed "Conferma" → almost certainly right;
- ``cancelled``: the user pressed "Annulla" → probably wrong, fix it by hand;
- ``readonly``/``rejected``/``unconfirmed``: review by hand.

Review the draft (fix ``expect_command``/``expect_arg``, delete noise) before
using it with ``benchmark_agent.py --cases`` or ``benchmark_semantic.py``.
Device and rule names are copied from ``--devices``/``--rules`` or left empty.

On the mini PC:
    .venv\\Scripts\\python.exe scripts\\export_agent_messages.py --out data\\agent_real_cases.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blackframe.agent import message_log  # noqa: E402
from blackframe.commands import COMMAND_REGISTRY  # noqa: E402
from blackframe.commands.naming import normalize_identifier  # noqa: E402


def build_cases(entries: list[dict]) -> list[dict]:
    outcome: dict[str, str] = {}
    for entry in entries:
        if entry.get("event") in {"confirm", "cancel"} and entry.get("pending_id"):
            outcome[entry["pending_id"]] = entry["event"]

    cases: dict[str, dict] = {}
    for entry in entries:
        if entry.get("event") != "proposal" or not entry.get("text"):
            continue
        command = entry.get("command") if entry.get("ok") else None
        if entry.get("pending_id"):
            tag = {"confirm": "confirmed", "cancel": "cancelled"}.get(
                outcome.get(entry["pending_id"], ""), "unconfirmed"
            )
        elif command is None:
            tag = "rejected"
        else:
            tag = "readonly" if COMMAND_REGISTRY[command].readonly else "unconfirmed"
        case = {
            "text": entry["text"],
            "expect_command": command,
            "tags": ["real", tag, f"source:{entry.get('source') or '?'}"],
        }
        if entry.get("arg") is not None:
            case["expect_arg"] = entry["arg"]
        # Last occurrence wins: it reflects the most recent agent version.
        cases[normalize_identifier(entry["text"])] = case
    return list(cases.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="data/agent_real_cases.json")
    parser.add_argument("--devices", default="", help="Nomi device separati da virgola")
    parser.add_argument("--rules", default="", help="Nomi regola separati da virgola")
    args = parser.parse_args()

    cases = build_cases(message_log.read_all())
    data = {
        "devices": [d for d in args.devices.split(",") if d],
        "rules": [r for r in args.rules.split(",") if r],
        "_note": "Bozza generata dal log dei messaggi: verificare a mano prima dell'uso.",
        "cases": cases,
    }
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tags: dict[str, int] = {}
    for case in cases:
        tags[case["tags"][1]] = tags.get(case["tags"][1], 0) + 1
    print(f"{len(cases)} frasi uniche scritte in {args.out}: {tags}")


if __name__ == "__main__":
    main()
