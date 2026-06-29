"""Production WSGI entrypoint for Windows.

gunicorn is Unix-only, so on Windows the app is served with waitress (a pure-
Python, cross-platform WSGI server). Importing ``blackframe.app`` runs
``load_dotenv()`` and ``create_app()``, so ``.env`` in the working directory is
read automatically and no service-level environment injection is needed.

Single worker semantics are preserved: waitress runs one process with a thread
pool, which matches the app's in-memory + file state model.

Setup (once):
    poetry run pip install waitress

Run:
    poetry run python deploy/serve_waitress.py

Register as a Windows service with NSSM (see docs/gestione_servizio.md).
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.runtime_paths import configure_runtime_environment

configure_runtime_environment()

from waitress import serve  # noqa: E402

from blackframe.app import app  # noqa: E402

if __name__ == "__main__":
    host = os.getenv("APP_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))
    threads = int(os.getenv("APP_WAITRESS_THREADS", "8"))
    serve(app, host=host, port=port, threads=threads)
