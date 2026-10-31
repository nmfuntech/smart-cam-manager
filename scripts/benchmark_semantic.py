#!/usr/bin/env python3
"""Benchmark of the semantic (embedding) intent classifier.

For each embedding model served by Ollama it embeds the example bank and the
evaluation phrases once, then sweeps the two confidence thresholds offline:

- coverage: share of in-scope phrases the classifier answers by itself
  (the rest goes to the LLM);
- precision: share of those answers with the right command and argument;
- out-of-scope phrases: how many are rejected, and how many would wrongly
  become a command (must stay 0 for the "safety" ones).

It suggests, per model, the thresholds with the highest coverage at a given
minimum precision. Nothing touches the app or the real data.

    poetry run python scripts/benchmark_semantic.py \\
        --models embeddinggemma,paraphrase-multilingual --cases scripts/agent_eval_cases.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blackframe.agent import intent, semantic  # noqa: E402
from blackframe.commands import COMMAND_REGISTRY  # noqa: E402

_DEFAULT_CASES = Path(__file__).with_name("agent_eval_cases.json")
_SCORES = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
_MARGINS = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10]


def _executable() -> set[str]:
    return {n for n, s in COMMAND_REGISTRY.items() if s.handler is not None}


def _evaluate(rows: list[dict], names: dict, min_score: float, margin: float) -> dict:
    accepted = correct = in_scope = 0
    oos = oos_rejected = oos_false = safety_false = 0
    for row in rows:
        match = semantic.decide(
            row["scores"], row["text"], row["allowed"], names, min_score=min_score, margin=margin
        )
        decision = None if match is None else (match.command, match.arg)
        expected = row["expect_command"]
        if expected is None:
            oos += 1
            if decision is not None and decision[0] == semantic.NO_COMMAND:
                oos_rejected += 1
            elif decision is not None:
                oos_false += 1
                if "safety" in row["tags"]:
                    safety_false += 1
            continue
        in_scope += 1
        if decision is None:
            continue
        accepted += 1
        arg_ok = "expect_arg" not in row or str(decision[1]) == str(row["expect_arg"])
        if decision[0] == expected and arg_ok:
            correct += 1
        else:
            row.setdefault("wrong", []).append((min_score, margin, decision))
    return {
        "min_score": min_score,
        "margin": margin,
        "coverage": accepted / in_scope if in_scope else 0.0,
        "precision": correct / accepted if accepted else 1.0,
        "accepted": accepted,
        "correct": correct,
        "oos_rejected": oos_rejected,
        "oos_false_accept": oos_false,
        "safety_false_accept": safety_false,
        "oos_total": oos,
    }


def _run_model(model: str, dataset: dict, min_precision: float) -> dict:
    os.environ["AGENT_EMBED_MODEL"] = model
    classifier = semantic.SemanticClassifier()
    started = time.perf_counter()
    if not classifier.build():
        return {"model": model, "error": "embedding non disponibili (modello scaricato?)"}
    build_s = time.perf_counter() - started

    names = {
        "device": dataset["devices"],
        "rule": dataset["rules"],
        "entity": dataset["devices"],
    }
    executable = _executable()
    rows, latencies = [], []
    for case in dataset["cases"]:
        text = case["text"]
        allowed = set(executable)
        if intent._is_info_question(text):
            allowed -= intent._MUTATING_COMMANDS
        t0 = time.perf_counter()
        scores = dict(classifier.scores(text) or [])
        latencies.append(time.perf_counter() - t0)
        rows.append({**case, "scores": scores, "allowed": allowed})

    grid = [_evaluate(rows, names, s, m) for s in _SCORES for m in _MARGINS]
    safe = [g for g in grid if g["safety_false_accept"] == 0 and g["precision"] >= min_precision]
    best = max(safe, key=lambda g: (g["coverage"], -g["oos_false_accept"]), default=None)
    latencies.sort()
    def topic_top1(row):
        match = semantic.decide(row["scores"], row["text"], row["allowed"], names,
                                min_score=-1.0, margin=-1.0)  # fmt: skip
        return None if match is None else match.command

    top1 = sum(1 for r in rows if r["expect_command"] and topic_top1(r) == r["expect_command"])
    in_scope = sum(1 for r in rows if r["expect_command"])
    misses = [
        {"text": r["text"], "expected": r["expect_command"], "got": topic_top1(r)}
        for r in rows
        if topic_top1(r) != (r["expect_command"] or semantic.NO_COMMAND)
    ]
    return {
        "model": model,
        "build_s": round(build_s, 1),
        "query_ms_p50": round(latencies[len(latencies) // 2] * 1000, 1),
        "query_ms_p95": round(latencies[int(len(latencies) * 0.95)] * 1000, 1),
        "top1_accuracy": round(top1 / in_scope, 3) if in_scope else None,
        "best": best,
        "misses": misses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", required=True, help="Modelli embedding separati da virgola")
    parser.add_argument("--cases", default=str(_DEFAULT_CASES))
    parser.add_argument("--min-precision", type=float, default=0.97)
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args()

    dataset = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    os.environ.setdefault("AGENT_SEMANTIC", "true")
    reports = []
    with tempfile.TemporaryDirectory() as tmp:
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            os.environ["AGENT_EMBED_CACHE_PATH"] = str(Path(tmp) / f"{model.replace(':', '_')}.npz")
            report = _run_model(model, dataset, args.min_precision)
            reports.append(report)
            print(f"=== {model} ===")
            if "error" in report:
                print(f"  {report['error']}\n")
                continue
            print(
                f"  esempi indicizzati in {report['build_s']} s, "
                f"query p50 {report['query_ms_p50']} ms / p95 {report['query_ms_p95']} ms"
            )
            print(f"  top-1 senza soglie (con regole): {report['top1_accuracy']:.1%}")
            best = report["best"]
            if best is None:
                print(f"  nessuna soglia raggiunge precisione >= {args.min_precision:.0%}\n")
                continue
            print(
                f"  soglie consigliate: min_score {best['min_score']}, margin {best['margin']}\n"
                f"    copertura {best['coverage']:.1%} ({best['accepted']} risposte), "
                f"precisione {best['precision']:.1%}\n"
                f"    fuori ambito: {best['oos_rejected']}/{best['oos_total']} rifiutate, "
                f"{best['oos_false_accept']} accettate per errore\n"
            )  # fmt: skip
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(reports, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
