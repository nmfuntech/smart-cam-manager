#!/usr/bin/env python3
"""Compare person/pet detectors on labelled frames (accuracy + latency).

Usage:
    poetry run python scripts/benchmark_detectors.py --images DIR --labels labels.csv \
        [--models ssd,yolox_nano,yolox_tiny] [--thresholds 0.35,0.45,0.55] [--runs 3]

``labels.csv`` columns: ``file,person,pet`` (1/0 for presence). Event covers
from ``captures/motion/*/*/cover.jpg`` make a good dataset: copy a few dozen
and label them by eye. The "primary" label follows the alert logic: person if
any person is visible, else pet, else nothing.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from blackframe.classification import (  # noqa: E402
    MobileNetSsdDetectorBackend,
    YoloxDetectorBackend,
)

MODELS = {
    "ssd": lambda thr: _SsdAdapter(thr),
    "yolox_nano": lambda thr: YoloxDetectorBackend(
        str(ROOT / "models/yolox_nano.onnx"), input_size=416, min_score=thr
    ),
    "yolox_tiny": lambda thr: YoloxDetectorBackend(
        str(ROOT / "models/yolox_tiny.onnx"), input_size=416, min_score=thr
    ),
}


class _SsdAdapter:
    """Expose MobileNet-SSD detections in the same (label, score, box) shape."""

    def __init__(self, thr: float) -> None:
        self.backend = MobileNetSsdDetectorBackend(
            str(ROOT / "models/ssd_mobilenet_v2_coco.pb"),
            str(ROOT / "models/ssd_mobilenet_v2_coco.pbtxt"),
            min_score=thr,
        )
        self.thr = thr

    def is_ready(self) -> bool:
        return self.backend.is_ready()

    def detect(self, frame):
        import numpy as np

        self.backend._ensure_loaded()
        blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (0, 0, 0), swapRB=True)
        self.backend.net.setInput(blob)
        rows = np.array(self.backend.net.forward()).reshape(-1, 7)
        out = []
        for row in rows:
            label = self.backend.COCO_LABELS.get(int(row[1]))
            if label and float(row[2]) >= self.thr:
                out.append((label, float(row[2]), tuple(row[3:7])))
        return out


def _primary(person: bool, pet: bool) -> str:
    return "persona" if person else ("animale" if pet else "niente")


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def evaluate(model, samples, runs: int) -> dict:
    stats = {"person": [0, 0, 0], "pet": [0, 0, 0]}
    primary_ok = 0
    latencies: list[float] = []
    errors: list[str] = []
    for path, truth_person, truth_pet in samples:
        frame = cv2.imread(str(path))
        detections = []
        for _ in range(runs):
            start = time.perf_counter()
            detections = model.detect(frame)
            latencies.append((time.perf_counter() - start) * 1000)
        pred_person = any(d[0] == "person" for d in detections)
        pred_pet = any(d[0] in ("cat", "dog") for d in detections)
        for key, truth, pred in (
            ("person", truth_person, pred_person),
            ("pet", truth_pet, pred_pet),
        ):
            if pred and truth:
                stats[key][0] += 1
            elif pred and not truth:
                stats[key][1] += 1
            elif truth and not pred:
                stats[key][2] += 1
        expected = _primary(truth_person, truth_pet)
        got = _primary(pred_person, pred_pet)
        if expected == got:
            primary_ok += 1
        else:
            errors.append(f"{path.name}: atteso {expected}, ottenuto {got}")
    latencies.sort()
    return {
        "person": _prf(*stats["person"]),
        "pet": _prf(*stats["pet"]),
        "primary": primary_ok / len(samples),
        "p50": statistics.median(latencies),
        "p95": latencies[int(len(latencies) * 0.95) - 1],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--models", default="ssd,yolox_nano,yolox_tiny")
    parser.add_argument("--thresholds", default="0.45")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--threads", type=int, default=0, help="cv2.setNumThreads (0 = auto)")
    parser.add_argument("--show-errors", action="store_true")
    args = parser.parse_args()
    if args.threads:
        cv2.setNumThreads(args.threads)

    samples = []
    with args.labels.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = args.images / row["file"]
            if path.is_file():
                samples.append((path, row["person"] == "1", row["pet"] == "1"))
    if not samples:
        print("Nessuna immagine trovata", file=sys.stderr)
        return 1
    print(f"{len(samples)} immagini, threads={cv2.getNumThreads()}\n")
    header = f"{'modello':<12}{'soglia':>7}{'persona P/R/F1':>22}{'animale P/R/F1':>22}"
    print(header + f"{'principale':>12}{'p50 ms':>9}{'p95 ms':>9}")
    for name in args.models.split(","):
        for thr in (float(t) for t in args.thresholds.split(",")):
            model = MODELS[name](thr)
            if not model.is_ready():
                print(f"{name:<12} modello assente")
                break
            model.detect(cv2.imread(str(samples[0][0])))  # warm-up
            res = evaluate(model, samples, args.runs)
            person = "{:.2f}/{:.2f}/{:.2f}".format(*res["person"])
            pet = "{:.2f}/{:.2f}/{:.2f}".format(*res["pet"])
            print(
                f"{name:<12}{thr:>7.2f}{person:>22}{pet:>22}"
                f"{res['primary']:>12.0%}{res['p50']:>9.0f}{res['p95']:>9.0f}"
            )
            if args.show_errors:
                for line in res["errors"]:
                    print(f"      {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
