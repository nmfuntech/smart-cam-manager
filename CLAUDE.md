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

Lint: `make lint` (ruff, line-length 100, rules E/F/I); `make lint-fix` to autofix.

## Architecture

Application code lives in **`src/blackframe/`** (Poetry package). User-editable config (e.g. automation rules YAML) is under **`config/`**. Runtime data: `data/`, `models/`, `captures/`. Tooling: `scripts/`, `deploy/`, `tests/`.

Entrypoints: `make run` / `poetry run python -m blackframe` (dev), `make serve` / `blackframe.app:app` (prod).

Three daemon threads run alongside Flask (all use `threading.Lock`, no `RLock`):

- **CameraStream** — reads RTSP frames with exponential backoff reconnection. Sets global FFMPEG env vars at module load that affect all `cv2.VideoCapture` calls in the process.
- **MotionDetector** — MOG2 background subtraction (`cv2.createBackgroundSubtractorMOG2`). Interdependent state: `trigger_streak`, `clear_streak`, `motion_detected`, `processed_frames` (+ `warmup_frames` from config). Threshold changes set `_needs_subtractor_rebuild`; the subtractor is rebuilt on the detection thread (MOG2 is stateful), not inside `apply_runtime_config`.
- **PTZController** — lazy-initialized on first move request via ONVIF. Errors are not surfaced until a button is pressed.

**Motion events** are saved as directories: `captures/motion/motion_event_YYYYMMDD_HHMMSS/`. On close the directory is renamed with a `__<category>` suffix (`persona`/`animale_domestico`/`movimento`) once the clip is finalized — the `motion_event_` prefix is preserved so globbing/retention keep working. An event is hidden from the API until a `.closed` marker file exists. Stale events are auto-closed based on file `mtime`. The API exposes a resolved `category` field (from `classification.detected_label`, then `class_label`, then the dir suffix) used by the UI event filter.

**Multi-camera**: the active profile uses the main camera/motion pair; profiles flagged `monitored` run background `CameraRuntime` workers. `/camera/<id>` renders a live-only view for a monitor via `/cam/<id>/...` endpoints; non-active, non-monitored profiles redirect to the active camera.

**Classification** is pluggable (Protocol): `local` (ONNX via `cv2.dnn`), `teachable_machine` (ONNX, `[-1,1]` input normalization), `cloud` (HTTP POST, http(s)-only SSRF guard). No model ships in-repo (`models/` absent), so `local` returns `unavailable` until a model is provided. Classification and notification dedup are persisted per-event in `meta.json` (`classification` / `notified` keys) and survive restarts; in-memory sets are only a fast path.

**Camera profiles** are stored as encrypted JSON (`data/camera_profiles.json`). Passwords use Fernet encryption with a fallback key chain: env var → keyfile → generated.

**RuntimeConfigManager** writes validated changes back to `.env`. Sensitive fields are redacted from the public API; `internal_only` fields reject API updates.

**Command registry** (`src/blackframe/commands/registry.py`) is the single source of truth for the ~35 channel-agnostic actions (status, toggles, PTZ, device/rule control...) usable by both Telegram slash commands and the agentic layer: `COMMAND_REGISTRY` maps a name to a `CommandSpec` (description, arg schema, `readonly` flag, handler). `TelegramCommandBot._dispatch` looks up commands here instead of hardcoding per-command branches; only Telegram-only meta commands (`start`/`help`/`menu`/`invite`/`guests`/`revoke`) and the async `clip` recording stay outside the registry.

**Agentic layer** (`src/blackframe/agent/`) lets a small local LLM (Ollama, e.g. `qwen2.5:0.5b` — sized for limited mini-PC hardware) turn free-text messages into one of the registry commands. Pipeline in `AgentService.propose()`: (1) a deterministic fast-path (`fastpath.py`, regex/keyword + `resolve_name`) answers frequent phrasings with zero LLM calls; (2) otherwise Ollama is called with structured outputs (`catalog.build_response_schema` — the command enum is grammar-constrained, plus a `"nessuno"` sentinel for out-of-scope; falls back to plain JSON mode on Ollama <0.5), few-shot example turns, and the last successful turn per chat/session (`context.py`, TTL) so follow-ups like "ora spegnila" resolve. Every proposal — fast-path or LLM — goes through the same `_validate_response`: whitelist against `COMMAND_REGISTRY` (a hallucinated name is always rejected) and `validate_arg`. Read-only commands execute immediately (and if the input looks like a question, `answer.py` composes a natural-Italian reply from the command output via a second LLM call, fail-open to raw text); state-changing commands create a `PendingIntent` (in-memory, TTL-based) requiring explicit `confirm`/`cancel` — this gate applies only to agent-suggested commands, never to commands typed directly. Entry points: Telegram free-text (inline-keyboard confirm) and the `/agente` web chat (`routes/agent.py`). Disabled by default (`AGENT_ENABLED=false`); fails closed if Ollama is unreachable; `start_warmup()` (called from `_build_agent`) preloads the model so the first message doesn't pay the disk-load. Never retry Ollama on timeout (only on connection refused/reset). Benchmark accuracy/latency across models with `poetry run python scripts/benchmark_agent.py --models a,b` (dataset: `scripts/agent_benchmark_cases.json`).

## Language

The UI, error messages, and some variable names are in **Italian**. Keep UI-facing strings in Italian (e.g., `"persona"`, `"animale_domestico"`, `"Errore apertura stream video"`).

## Environment

All configuration is via environment variables. Required at startup: `APP_ADMIN_PASSWORD`, `APP_SECRET_KEY`, `TAPO_USERNAME`, `TAPO_PASSWORD`. See `.env.example` for the full list with defaults and documentation.

## Gotchas

- `MOTION_BLUR_SIZE` is silently incremented to the next odd number if even — don't treat the env value as canonical.
- `save_frame()` returns `(None, None)` when `save_frames=false`; callers must check.
- Recordings: OpenCV often lacks an H.264 encoder and writes mp4v (not browser-playable). `finalize_recording(transcode=True)` re-encodes event & on-demand clips to H.264 via ffmpeg; continuous segments stay mp4v. Needs `ffmpeg`/`ffprobe` on PATH.
- **Windows**: OpenCV's bundled OpenH264 DLL is broken (`Incorrect library version loaded`); recording uses mp4v only and relies on ffmpeg transcode for browser playback. Install ffmpeg with `winget install Gyan.FFmpeg` or `.\blackframe.ps1 install-windows`. Run `.\blackframe.ps1 check-prerequisites` after install. On Windows use `blackframe.ps1` instead of `make`. Build installer: `.\blackframe.ps1 build-installer` (requires Inno Setup 6).
- No database — all state is files + in-memory in one process. Gunicorn must stay single-worker (`deploy/gunicorn.conf.py`); extra workers spawn duplicate camera/motion threads and corrupt event dirs.

## Git

Commit messages follow Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.
