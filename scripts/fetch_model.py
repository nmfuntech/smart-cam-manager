#!/usr/bin/env python3
"""Scarica i modelli di detection persona/animale nella cartella models/.

Il repo non include modelli. Di default scarica YOLOX-Tiny (backend `yolox`,
consigliato, licenza Apache-2.0); `--model nano` scarica YOLOX-Nano (più
leggero, meno preciso sulle persone); `--model ssd` il vecchio MobileNet-SSD v2
(backend `detection`). Ogni file è verificato via SHA256 e lo script è
idempotente: un file già presente e valido viene saltato.

Uso:
    python scripts/fetch_model.py                 # YOLOX-Tiny in ./models
    python scripts/fetch_model.py --model nano
    python scripts/fetch_model.py --model ssd
    python scripts/fetch_model.py --force         # riscarica anche se presente
"""

import argparse
import hashlib
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# I pesi ufficiali sono distribuiti come tarball dal model zoo TensorFlow; il file
# di configurazione del grafo (pbtxt) per OpenCV è ospitato in opencv_extra.
WEIGHTS_TARBALL_URL = (
    "http://download.tensorflow.org/models/object_detection/ssd_mobilenet_v2_coco_2018_03_29.tar.gz"
)
WEIGHTS_MEMBER = "ssd_mobilenet_v2_coco_2018_03_29/frozen_inference_graph.pb"
CONFIG_URL = (
    "https://raw.githubusercontent.com/opencv/opencv_extra/master/"
    "testdata/dnn/ssd_mobilenet_v2_coco_2018_03_29.pbtxt"
)

WEIGHTS_FILENAME = "ssd_mobilenet_v2_coco.pb"
CONFIG_FILENAME = "ssd_mobilenet_v2_coco.pbtxt"

YOLOX_RELEASE = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
YOLOX_MODELS = {
    "tiny": (
        "yolox_tiny.onnx",
        "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
    ),
    "nano": (
        "yolox_nano.onnx",
        "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
    ),
}

# SHA256 verificati al momento dello sviluppo (vedi README/commit).
WEIGHTS_SHA256 = "2a8d8a89d695842e60d8c6d144181100555563e21acf2fa1e8f561fec5c3c6ad"
CONFIG_SHA256 = "cfbecf9447c384403ef5cf695f4cd0bb4840c1312938280350639a9a8e82d303"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _already_valid(path: Path, expected_sha: str) -> bool:
    return path.exists() and _sha256(path) == expected_sha


def _verify(path: Path, expected_sha: str) -> None:
    actual = _sha256(path)
    if actual != expected_sha:
        path.unlink(missing_ok=True)
        raise SystemExit(
            f"Checksum non valido per {path.name}: atteso {expected_sha}, ottenuto {actual}"
        )


def _fetch_config(dest: Path) -> None:
    print(f"Scarico {CONFIG_FILENAME} ...")
    with urllib.request.urlopen(CONFIG_URL, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    _verify(dest, CONFIG_SHA256)
    print(f"  -> {dest} (ok)")


def _fetch_weights(dest: Path) -> None:
    print(f"Scarico {WEIGHTS_FILENAME} (tarball ~180MB, una tantum) ...")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tarball_path = Path(tmp.name)
    try:
        urllib.request.urlretrieve(WEIGHTS_TARBALL_URL, tarball_path)
        with tarfile.open(tarball_path, "r:gz") as tar:
            member = tar.extractfile(WEIGHTS_MEMBER)
            if member is None:
                raise SystemExit(f"Membro non trovato nel tarball: {WEIGHTS_MEMBER}")
            dest.write_bytes(member.read())
    finally:
        tarball_path.unlink(missing_ok=True)
    _verify(dest, WEIGHTS_SHA256)
    print(f"  -> {dest} (ok)")


def _yolox_fetcher(filename: str, expected_sha: str):
    def fetch(dest: Path) -> None:
        print(f"Scarico {filename} ...")
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(f"{YOLOX_RELEASE}/{filename}", tmp)
        _verify(tmp, expected_sha)
        tmp.replace(dest)
        print(f"  -> {dest} (ok)")

    return fetch


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default="models",
        help="Cartella di destinazione (default: ./models)",
    )
    parser.add_argument(
        "--model",
        choices=("tiny", "nano", "ssd"),
        default="tiny",
        help="Modello da scaricare (default: tiny = YOLOX-Tiny)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Riscarica anche se i file sono già presenti e validi",
    )
    args = parser.parse_args(argv)

    models_dir = Path(args.dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    if args.model == "ssd":
        targets = [
            (models_dir / WEIGHTS_FILENAME, WEIGHTS_SHA256, _fetch_weights),
            (models_dir / CONFIG_FILENAME, CONFIG_SHA256, _fetch_config),
        ]
        backend_hint = "CLASSIFICATION_BACKEND=detection"
    else:
        filename, sha = YOLOX_MODELS[args.model]
        targets = [(models_dir / filename, sha, _yolox_fetcher(filename, sha))]
        backend_hint = "CLASSIFICATION_BACKEND=yolox"
        if args.model == "nano":
            backend_hint += f" e CLASSIFICATION_YOLOX_MODEL_PATH=models/{filename}"

    for path, expected_sha, fetch in targets:
        if not args.force and _already_valid(path, expected_sha):
            print(f"{path.name} già presente e valido, salto.")
            continue
        fetch(path)

    print(f"\nModello pronto. Imposta CLASSIFICATION_ENABLED=true e {backend_hint}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
