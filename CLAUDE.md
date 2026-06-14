# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**BLACKFRAME** — Flask web app for Tapo IP camera monitoring with motion detection and optional person/pet classification. Python 3.11+, managed with Poetry.

## Commands

```bash
make install   # install dependencies via Poetry
make run       # start app on localhost:8000 (Flask dev)
make serve     # production server (gunicorn, single worker — see Gotchas)
make test      # run full test suite (pytest)
```

Run a single test or class:
```bash
poetry run pytest tests/test_app.py::ClassName::test_method -v
poetry run pytest -k "test_name" -v
```

Lint (no make target): `poetry run ruff check .` — ruff, line-length 100, rules E/F/I.

## Architecture

Three daemon threads run alongside Flask (all use `threading.Lock`, no `RLock`):

- **CameraStream** — reads RTSP frames with exponential backoff reconnection. Sets global FFMPEG env vars at module load that affect all `cv2.VideoCapture` calls in the process.
- **MotionDetector** — MOG2 background subtraction (`cv2.createBackgroundSubtractorMOG2`). Interdependent state: `trigger_streak`, `clear_streak`, `motion_detected`, `processed_frames` (+ `warmup_frames` from config). Threshold changes set `_needs_subtractor_rebuild`; the subtractor is rebuilt on the detection thread (MOG2 is stateful), not inside `apply_runtime_config`.
- **PTZController** — lazy-initialized on first move request via ONVIF. Errors are not surfaced until a button is pressed.

**Motion events** are saved as directories: `captures/motion/motion_event_YYYYMMDD_HHMMSS/`. An event is hidden from the API until a `.closed` marker file exists. Stale events are auto-closed based on file `mtime`.

**Multi-camera**: the active profile uses the main camera/motion pair; profiles flagged `monitored` run background `CameraRuntime` workers. `/camera/<id>` renders a live-only view for a monitor via `/cam/<id>/...` endpoints; non-active, non-monitored profiles redirect to the active camera.

**Classification** is pluggable (Protocol): `local` (ONNX via `cv2.dnn`), `teachable_machine` (ONNX, `[-1,1]` input normalization), `cloud` (HTTP POST, http(s)-only SSRF guard). No model ships in-repo (`models/` absent), so `local` returns `unavailable` until a model is provided. Classification and notification dedup are persisted per-event in `meta.json` (`classification` / `notified` keys) and survive restarts; in-memory sets are only a fast path.

**Camera profiles** are stored as encrypted JSON (`data/camera_profiles.json`). Passwords use Fernet encryption with a fallback key chain: env var → keyfile → generated.

**RuntimeConfigManager** writes validated changes back to `.env`. Sensitive fields are redacted from the public API; `internal_only` fields reject API updates.

## Language

The UI, error messages, and some variable names are in **Italian**. Keep UI-facing strings in Italian (e.g., `"persona"`, `"animale_domestico"`, `"Errore apertura stream video"`).

## Environment

All configuration is via environment variables. Required at startup: `APP_ADMIN_PASSWORD`, `APP_SECRET_KEY`, `TAPO_USERNAME`, `TAPO_PASSWORD`. See `.env.example` for the full list with defaults and documentation.

## Gotchas

- `MOTION_BLUR_SIZE` is silently incremented to the next odd number if even — don't treat the env value as canonical.
- `save_frame()` returns `(None, None)` when `save_frames=false`; callers must check.
- Recordings: OpenCV often lacks an H.264 encoder and writes mp4v (not browser-playable). `finalize_recording(transcode=True)` re-encodes event & on-demand clips to H.264 via ffmpeg; continuous segments stay mp4v. Needs `ffmpeg`/`ffprobe` on PATH.
- No database — all state is files + in-memory in one process. Gunicorn must stay single-worker (`deploy/gunicorn.conf.py`); extra workers spawn duplicate camera/motion threads and corrupt event dirs.

## Git

Commit messages follow Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.
