#!/usr/bin/env python3
"""Benchmark dell'agente NLU (frase libera -> comando del registry).

Misura, contro un'istanza Ollama reale, quanto bene ``intent.interpret``
traduce frasi italiane nei comandi del ``COMMAND_REGISTRY``:

- accuratezza sul nome comando e sull'argomento;
- tasso di falsi-accettati sulle frasi fuori ambito (metrica di sicurezza:
  una frase come "formatta il disco" non deve MAI produrre un comando);
- latenza p50/p95/max, con il primo colpo (modello freddo) riportato a parte.

Confronta piu' modelli nella stessa esecuzione, cosi' la scelta del modello
per il mini PC si fa con i numeri e non a sensazione:

    poetry run python scripts/benchmark_agent.py --models qwen2.5:0.5b,qwen3:0.6b

I flag ``--no-fastpath`` / ``--no-schema`` / ``--no-examples`` disattivano le
singole ottimizzazioni (via env) per misurarne il contributo in A/B.

Il dataset vive in ``scripts/agent_benchmark_cases.json``: nomi device/regola
finti coerenti tra prompt e validazione, casi con campo ``context`` per i
follow-up (saltati se la versione di ``interpret`` non supporta ancora
``last_turn``). Non tocca ne' l'app ne' i dati reali: i servizi sono stub.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blackframe.agent import catalog, intent  # noqa: E402
from blackframe.commands import registry as commands_registry  # noqa: E402

_CASES_PATH = Path(__file__).resolve().parent / "agent_benchmark_cases.json"
# Frase volutamente NON coperta dal fast-path deterministico: serve a forzare
# una vera chiamata LLM per misurare la latenza a modello freddo.
_COLD_PROBE = "vorrei sapere come procede il monitoraggio della casa"


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def device_names(self) -> list[str]:
        return list(self._names)


class _FakeServices:
    def __init__(self, device_names: list[str]) -> None:
        self.automation_registry = _FakeRegistry(device_names)


def _check_ollama(base_url: str) -> None:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5):
            pass
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"Ollama non raggiungibile su {base_url} ({exc}). Avvialo e riprova.")


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _build_last_turn(context: dict):
    """Costruisce il LastTurn per i casi follow-up, se il modulo esiste gia'."""
    try:
        from blackframe.agent.context import LastTurn
    except ImportError:
        return None
    return LastTurn(
        user_text=context["user"],
        command=context["command"],
        arg=context.get("arg"),
        created_at=time.monotonic(),
    )


def _run_case(case: dict, services: _FakeServices, supports_context: bool) -> dict:
    kwargs: dict = {"services": services}
    context = case.get("context")
    if context:
        if not supports_context:
            return {"skipped": True, "reason": "interpret() senza supporto last_turn"}
        last_turn = _build_last_turn(context)
        if last_turn is None:
            return {"skipped": True, "reason": "modulo context non ancora presente"}
        kwargs["last_turn"] = last_turn

    start = time.perf_counter()
    suggestion = intent.interpret(case["text"], **kwargs)
    elapsed = time.perf_counter() - start

    expect_command = case.get("expect_command")
    if expect_command is None:
        command_ok = not suggestion.ok
        false_accept = suggestion.ok
    else:
        command_ok = suggestion.ok and suggestion.command == expect_command
        false_accept = False

    arg_checked = "expect_arg" in case and command_ok
    arg_ok = arg_checked and suggestion.arg == case.get("expect_arg")

    return {
        "skipped": False,
        "latency": elapsed,
        "expected_null": expect_command is None,
        "command_ok": command_ok,
        "false_accept": false_accept,
        "arg_checked": arg_checked,
        "arg_ok": arg_ok,
        "got_command": suggestion.command if suggestion.ok else None,
        "got_arg": suggestion.arg if suggestion.ok else None,
    }


def _run_model(model: str, dataset: dict, repeats: int) -> dict:
    services = _FakeServices(dataset["devices"])
    supports_context = "last_turn" in inspect.signature(intent.interpret).parameters

    os.environ["AGENT_OLLAMA_MODEL"] = model

    cold_start = time.perf_counter()
    intent.interpret(_COLD_PROBE, services=services)
    cold_latency = time.perf_counter() - cold_start

    latencies: list[float] = []
    per_case: list[dict] = []
    skipped = 0
    tag_totals: dict[str, list[int]] = {}

    for _ in range(repeats):
        for case in dataset["cases"]:
            outcome = _run_case(case, services, supports_context)
            if outcome["skipped"]:
                skipped += 1
                continue
            latencies.append(outcome["latency"])
            per_case.append({"text": case["text"], **outcome})
            for tag in case.get("tags", []):
                hit_total = tag_totals.setdefault(tag, [0, 0])
                hit_total[0] += 1 if outcome["command_ok"] else 0
                hit_total[1] += 1

    command_cases = [c for c in per_case if not c["expected_null"]]
    oos_cases = [c for c in per_case if c["expected_null"]]
    arg_cases = [c for c in per_case if c["arg_checked"]]

    return {
        "model": model,
        "cases_run": len(per_case),
        "cases_skipped": skipped,
        "command_accuracy": (
            sum(1 for c in command_cases if c["command_ok"]) / len(command_cases)
            if command_cases
            else 0.0
        ),
        "arg_accuracy": (
            sum(1 for c in arg_cases if c["arg_ok"]) / len(arg_cases) if arg_cases else None
        ),
        "oos_false_accept_rate": (
            sum(1 for c in oos_cases if c["false_accept"]) / len(oos_cases) if oos_cases else None
        ),
        "latency_cold_s": round(cold_latency, 3),
        "latency_p50_s": round(_percentile(latencies, 50), 3),
        "latency_p95_s": round(_percentile(latencies, 95), 3),
        "latency_max_s": round(max(latencies), 3) if latencies else 0.0,
        "latency_mean_s": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "per_tag_accuracy": {
            tag: round(hit / total, 3) for tag, (hit, total) in sorted(tag_totals.items())
        },
        "failures": [
            {
                "text": c["text"],
                "got_command": c["got_command"],
                "got_arg": c["got_arg"],
            }
            for c in per_case
            if not c["command_ok"] or (c["arg_checked"] and not c["arg_ok"])
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models",
        default=os.getenv("AGENT_OLLAMA_MODEL", "qwen2.5:0.5b"),
        help="Modelli da confrontare, separati da virgola (default: env o qwen2.5:0.5b)",
    )
    parser.add_argument("--url", default=os.getenv("AGENT_OLLAMA_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--repeats", type=int, default=1, help="Ripetizioni del dataset")
    parser.add_argument("--cases", default=str(_CASES_PATH), help="Percorso dataset JSON")
    parser.add_argument("--json", dest="json_out", help="Salva il report completo su file JSON")
    parser.add_argument("--no-fastpath", action="store_true", help="Disattiva AGENT_FASTPATH")
    parser.add_argument("--no-schema", action="store_true", help="Disattiva AGENT_SCHEMA_FORMAT")
    parser.add_argument(
        "--no-examples", action="store_true", help="Disattiva AGENT_PROMPT_EXAMPLES"
    )
    args = parser.parse_args()

    dataset = json.loads(Path(args.cases).read_text(encoding="utf-8"))

    os.environ["AGENT_OLLAMA_URL"] = args.url
    if args.no_fastpath:
        os.environ["AGENT_FASTPATH"] = "false"
    if args.no_schema:
        os.environ["AGENT_SCHEMA_FORMAT"] = "false"
    if args.no_examples:
        os.environ["AGENT_PROMPT_EXAMPLES"] = "false"

    _check_ollama(args.url)

    known_names = {"device": dataset["devices"], "rule": dataset["rules"]}
    prompt = catalog.build_system_prompt(known_names=known_names)
    print(f"Prompt di sistema: {len(prompt)} caratteri (~{len(prompt) // 4} token)")
    print(f"Casi: {len(dataset['cases'])} x {args.repeats} ripetizioni\n")

    fake_rules = [{"name": name} for name in dataset["rules"]]
    reports = []
    # Le regole arrivano da load_rules_raw sia in intent (grounding prompt)
    # sia in registry (validate_arg): vanno stubbate entrambe le importazioni.
    with (
        mock.patch.object(intent, "load_rules_raw", return_value=fake_rules),
        mock.patch.object(commands_registry, "load_rules_raw", return_value=fake_rules),
    ):
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            print(f"=== {model} ===")
            report = _run_model(model, dataset, args.repeats)
            reports.append(report)
            arg_acc = report["arg_accuracy"]
            oos_rate = report["oos_false_accept_rate"]
            print(f"  comando:      {report['command_accuracy']:.1%}")
            print(
                f"  argomento:    {arg_acc:.1%}" if arg_acc is not None else "  argomento:    n/d"
            )
            print(
                f"  falsi accett: {oos_rate:.1%}" if oos_rate is not None else "  falsi accett: n/d"
            )
            print(
                f"  latenza:      cold {report['latency_cold_s']}s | "
                f"p50 {report['latency_p50_s']}s | p95 {report['latency_p95_s']}s | "
                f"max {report['latency_max_s']}s"
            )
            if report["cases_skipped"]:
                print(f"  saltati:      {report['cases_skipped']} (context non supportato)")
            if report["failures"]:
                print(f"  errori ({len(report['failures'])}):")
                for failure in report["failures"][:15]:
                    print(
                        f"    - {failure['text']!r} -> "
                        f"{failure['got_command']} / {failure['got_arg']}"
                    )
            print()

    if args.json_out:
        payload = {
            "prompt_chars": len(prompt),
            "repeats": args.repeats,
            "env": {
                key: os.environ.get(key)
                for key in ("AGENT_FASTPATH", "AGENT_SCHEMA_FORMAT", "AGENT_PROMPT_EXAMPLES")
            },
            "models": reports,
        }
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Report salvato in {args.json_out}")


if __name__ == "__main__":
    main()
