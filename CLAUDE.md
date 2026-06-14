# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**BLACKFRAME** — Flask web app for Tapo IP camera monitoring with motion detection and optional person/pet classification. Python 3.11+, managed with Poetry.

## Commands

```bash
make install   # install dependencies via Poetry
make run       # start app on localhost:8000
make test      # run full test suite (unittest)
```

Run a single test or class:
```bash
poetry run pytest tests/test_app.py::ClassName::test_method -v
poetry run pytest -k "test_name" -v
```

## Architecture

Three daemon threads run alongside Flask (all use `threading.Lock`, no `RLock`):

- **CameraStream** — reads RTSP frames with exponential backoff reconnection. Sets global FFMPEG env vars at module load that affect all `cv2.VideoCapture` calls in the process.
- **MotionDetector** — background subtraction with six interdependent state counters (`trigger_streak`, `clear_streak`, `motion_detected`, `background_frame`, `processed_frames`, `warmup_count`). Changing thresholds resets background and counters.
- **PTZController** — lazy-initialized on first move request via ONVIF. Errors are not surfaced until a button is pressed.

**Motion events** are saved as directories: `captures/motion/motion_event_YYYYMMDD_HHMMSS/`. An event is hidden from the API until a `.closed` marker file exists. Stale events are auto-closed based on file `mtime`.

**Classification** is pluggable (Protocol class): `local` (ONNX via `cv2.dnn`), `teachable_machine` (stub), `cloud` (stub). Classification dedup uses an in-memory set — cleared on restart.

**Camera profiles** are stored as encrypted JSON (`data/camera_profiles.json`). Passwords use Fernet encryption with a fallback key chain: env var → keyfile → generated.

**RuntimeConfigManager** writes validated changes back to `.env`. Sensitive fields are redacted from the public API; `internal_only` fields reject API updates.

## Language

The UI, error messages, and some variable names are in **Italian**. Keep UI-facing strings in Italian (e.g., `"persona"`, `"animale_domestico"`, `"Errore apertura stream video"`).

## Environment

All configuration is via environment variables. Required at startup: `APP_ADMIN_PASSWORD`, `APP_SECRET_KEY`, `TAPO_USERNAME`, `TAPO_PASSWORD`. See `.env.example` for the full list with defaults and documentation.

## Gotchas

- `MOTION_BLUR_SIZE` is silently incremented to the next odd number if even — don't treat the env value as canonical.
- `save_frame()` returns `(None, None)` when `save_frames=false`; callers must check.
- No database — all state is files + in-memory. Multi-process deployment would require redesign.

## Git

Commit messages follow Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.
