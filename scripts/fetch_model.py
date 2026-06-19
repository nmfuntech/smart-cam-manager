#!/usr/bin/env python3
"""Scarica il modello di detection persona/animale (MobileNet-SSD v2 COCO).

Il repo non include modelli. Questo script scarica i due file necessari al
backend `detection` (OpenCV DNN) nella cartella `models/`, verificando lo SHA256
di ognuno. È idempotente: se un file è già presente e con hash corretto, lo salta.

Uso:
    python scripts/fetch_model.py            # scarica in ./models
    python scripts/fetch_model.py --force    # riscarica anche se già presente
    python scripts/fetch_model.py --dir /path/models
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default="models",
        help="Cartella di destinazione (default: ./models)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Riscarica anche se i file sono già presenti e validi",
    )
    args = parser.parse_args(argv)

    models_dir = Path(args.dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        (models_dir / WEIGHTS_FILENAME, WEIGHTS_SHA256, _fetch_weights),
        (models_dir / CONFIG_FILENAME, CONFIG_SHA256, _fetch_config),
    ]

    for path, expected_sha, fetch in targets:
        if not args.force and _already_valid(path, expected_sha):
            print(f"{path.name} già presente e valido, salto.")
            continue
        fetch(path)

    print(
        "\nModello pronto. Imposta CLASSIFICATION_ENABLED=true e CLASSIFICATION_BACKEND=detection."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
