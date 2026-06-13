"""Gunicorn configuration for BLACKFRAME.

IMPORTANT: run with a single worker. All application state (motion counters,
event store, classification/notification dedup, rate limiter) lives in-memory and
in local files within one process. Multiple workers would each spawn their own
camera/motion/recorder threads and corrupt shared event directories.

Concurrency for the (mostly I/O-bound: MJPEG streaming, snapshots) HTTP handlers
is provided by threads, not workers.

Start with:  poetry run gunicorn -c deploy/gunicorn.conf.py app:app
"""

import os

bind = f"{os.getenv('APP_BIND_HOST', '127.0.0.1')}:{os.getenv('APP_PORT', '8000')}"
workers = 1
threads = int(os.getenv("APP_GUNICORN_THREADS", "8"))
worker_class = "gthread"
# MJPEG streams are long-lived; keep generous timeouts so they are not killed.
timeout = int(os.getenv("APP_GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("APP_LOG_LEVEL", "info")
