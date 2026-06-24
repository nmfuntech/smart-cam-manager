#!/usr/bin/env bash
set -Eeuo pipefail

# Bootstrap BLACKFRAME on Windows from Git Bash / MSYS / WSL with Windows interop.
# Installs/checks Python 3.11+, Git, Poetry, Visual C++ runtime, project deps,
# then prepares a Windows .bat launcher.

APP_NAME="BLACKFRAME"
APP_PORT="${APP_PORT:-8000}"
RUN_APP=0
SETUP_ENV=0
MINIMAL_ENV=1
OPEN_FIREWALL=0
INSTALL_TOOLS=1
INSTALL_DEPS=1
INSTALL_VCREDIST=1
INSTALL_FFMPEG=1
FETCH_MODEL=1
TUNE_MINI_PC=0

PYTHON_MIN_MINOR=11

log() {
  printf '\n[%s] %s\n' "$APP_NAME" "$*"
}

warn() {
  printf '\n[%s] WARNING: %s\n' "$APP_NAME" "$*" >&2
}

die() {
  printf '\n[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage:
  scripts/bootstrap_windows.sh [opzioni]

Opzioni:
  --run              avvia app alla fine
  --setup-env        esegui setup interattivo .env
  --full-env         con --setup-env, chiede anche valori non essenziali
  --firewall         prova ad aprire porta APP_PORT nel firewall Windows
  --no-tools         non installa Python/Git/Poetry/VC++ runtime
  --no-deps          non esegue poetry install
  --no-vcredist      non installa Microsoft Visual C++ Redistributable
  --no-ffmpeg        non installa ffmpeg via winget
  --no-model         non scarica il modello classificazione
  --tune-mini-pc     applica profilo tuning mini PC al .env esistente
  -h, --help         mostra questa guida

Variabili:
  APP_PORT=8000      porta app/firewall

Esempi:
  scripts/bootstrap_windows.sh --setup-env --run
  APP_PORT=8000 scripts/bootstrap_windows.sh --firewall
USAGE
}

while (($#)); do
  case "$1" in
    --run)
      RUN_APP=1
      ;;
    --setup-env)
      SETUP_ENV=1
      ;;
    --full-env)
      MINIMAL_ENV=0
      ;;
    --firewall)
      OPEN_FIREWALL=1
      ;;
    --no-tools)
      INSTALL_TOOLS=0
      INSTALL_VCREDIST=0
      ;;
    --no-deps)
      INSTALL_DEPS=0
      ;;
    --no-vcredist)
      INSTALL_VCREDIST=0
      ;;
    --no-ffmpeg)
      INSTALL_FFMPEG=0
      ;;
    --no-model)
      FETCH_MODEL=0
      ;;
    --tune-mini-pc)
      TUNE_MINI_PC=1
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      die "Opzione sconosciuta: $1"
      ;;
  esac
  shift
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

case "$(uname -s | tr '[:upper:]' '[:lower:]')" in
  mingw* | msys* | cygwin* | linux*)
    ;;
  *)
    warn "Ambiente non riconosciuto. Script pensato per Windows Git Bash/MSYS/WSL."
    ;;
esac

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

win_path() {
  local path="$1"
  if has_cmd cygpath; then
    cygpath -aw "$path"
  elif pwd -W >/dev/null 2>&1; then
    (cd "$path" && pwd -W)
  else
    printf '%s' "$path"
  fi
}

unix_path() {
  local path="$1"
  if has_cmd cygpath; then
    cygpath -u "$path"
  else
    printf '%s' "$path"
  fi
}

run_ps() {
  local exe="powershell.exe"
  if ! has_cmd "$exe"; then
    exe="pwsh.exe"
  fi
  has_cmd "$exe" || die "PowerShell non trovato. Usa Git Bash su Windows o WSL con interop."
  "$exe" -NoProfile -ExecutionPolicy Bypass -Command "$1"
}

winget_available() {
  has_cmd winget.exe || has_cmd winget
}

winget_install() {
  local package_id="$1"
  local label="$2"
  local winget_bin="winget"
  if has_cmd winget.exe; then
    winget_bin="winget.exe"
  fi

  log "Installo/verifico $label via winget"
  "$winget_bin" install \
    --id "$package_id" \
    --exact \
    --source winget \
    --accept-package-agreements \
    --accept-source-agreements || warn "$label non installato via winget. Proseguo se già presente."
}

append_known_windows_paths() {
  local candidates=()

  # Python 3.11, 3.12, 3.13 — cerca dal più recente
  if [[ -n "${LOCALAPPDATA:-}" ]]; then
    for minor in 313 312 311; do
      candidates+=(
        "$LOCALAPPDATA\\Programs\\Python\\Python${minor}"
        "$LOCALAPPDATA\\Programs\\Python\\Python${minor}\\Scripts"
      )
    done
  fi

  if [[ -n "${APPDATA:-}" ]]; then
    candidates+=("$APPDATA\\Python\\Scripts")
  fi

  for minor in 313 312 311; do
    candidates+=(
      "C:\\Program Files\\Python${minor}"
      "C:\\Program Files\\Python${minor}\\Scripts"
    )
  done
  candidates+=("C:\\Program Files\\Git\\cmd")

  local candidate unix_candidate
  for candidate in "${candidates[@]}"; do
    unix_candidate="$(unix_path "$candidate")"
    if [[ -d "$unix_candidate" ]]; then
      case ":$PATH:" in
        *":$unix_candidate:"*) ;;
        *) export PATH="$unix_candidate:$PATH" ;;
      esac
    fi
  done
}

ensure_user_path_contains() {
  local windows_dir="$1"
  run_ps "\$dir = '$windows_dir'; \$old = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not \$old) { \$old = '' }; \$parts = \$old -split ';' | Where-Object { \$_ }; if (\$parts -notcontains \$dir) { [Environment]::SetEnvironmentVariable('Path', (\$old.TrimEnd(';') + ';' + \$dir).TrimStart(';'), 'User') }" >/dev/null
}

PYTHON_CMD=()
POETRY_CMD=()

python_is_ok() {
  "$@" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, $PYTHON_MIN_MINOR) else 1)" >/dev/null 2>&1
}

find_python() {
  append_known_windows_paths

  # Python Launcher (preferito su Windows: seleziona la versione esplicitamente)
  for ver in 3.13 3.12 3.11; do
    if has_cmd py && python_is_ok py -"$ver"; then
      PYTHON_CMD=(py -"$ver")
      return 0
    fi
  done

  if has_cmd python && python_is_ok python; then
    PYTHON_CMD=(python)
    return 0
  fi

  if has_cmd python3 && python_is_ok python3; then
    PYTHON_CMD=(python3)
    return 0
  fi

  # percorsi noti per 3.13, 3.12, 3.11
  local candidates=()
  for minor in 313 312 311; do
    if [[ -n "${LOCALAPPDATA:-}" ]]; then
      candidates+=("$LOCALAPPDATA\\Programs\\Python\\Python${minor}\\python.exe")
    fi
    candidates+=("C:\\Program Files\\Python${minor}\\python.exe")
  done

  local candidate unix_candidate
  for candidate in "${candidates[@]}"; do
    unix_candidate="$(unix_path "$candidate")"
    if [[ -x "$unix_candidate" ]] && python_is_ok "$unix_candidate"; then
      PYTHON_CMD=("$unix_candidate")
      return 0
    fi
  done

  return 1
}

find_poetry() {
  append_known_windows_paths

  if has_cmd poetry; then
    POETRY_CMD=(poetry)
    return 0
  fi

  local candidates=()
  if [[ -n "${APPDATA:-}" ]]; then
    candidates+=("$APPDATA\\Python\\Scripts\\poetry.exe")
  fi

  local candidate unix_candidate
  for candidate in "${candidates[@]}"; do
    unix_candidate="$(unix_path "$candidate")"
    if [[ -x "$unix_candidate" ]]; then
      POETRY_CMD=("$unix_candidate")
      return 0
    fi
  done

  return 1
}

install_poetry() {
  log "Installo Poetry"

  # Niente 'curl | python': scarica l'installer su file e poi eseguilo. Così un
  # MITM o un errore HTTP non vengono eseguiti come codice.
  if has_cmd curl; then
    local installer
    installer="$(mktemp --suffix=.py 2>/dev/null || printf '%s' "${TEMP:-${TMP:-/tmp}}/poetry_install_$$.py")"
    # shellcheck disable=SC2064
    trap "rm -f '$installer'" RETURN
    if ! curl -fsSL --proto '=https' --tlsv1.2 \
          https://install.python-poetry.org -o "$installer"; then
      die "Download dell'installer Poetry fallito."
    fi
    "${PYTHON_CMD[@]}" "$installer"
  else
    # Fallback PowerShell: scarica su file temporaneo, poi esegue
    run_ps "\$tmp = [System.IO.Path]::GetTempFileName() + '.py'; Invoke-WebRequest -Uri 'https://install.python-poetry.org' -OutFile \$tmp -UseBasicParsing; python \$tmp; Remove-Item \$tmp"
  fi

  if [[ -n "${APPDATA:-}" ]]; then
    ensure_user_path_contains "$APPDATA\\Python\\Scripts"
  else
    warn "APPDATA non disponibile: aggiungi manualmente Poetry al PATH utente."
  fi

  append_known_windows_paths
}

ensure_tools() {
  append_known_windows_paths

  if ! find_python; then
    if ((INSTALL_TOOLS)) && winget_available; then
      winget_install "Python.Python.3.11" "Python 3.11"
      append_known_windows_paths
    else
      die "Python 3.${PYTHON_MIN_MINOR}+ mancante. Installa Python 3.11+ x64 e ripeti."
    fi
  fi
  find_python || die "Python 3.${PYTHON_MIN_MINOR}+ non trovato dopo installazione. Apri nuovo terminale e ripeti."
  log "Python OK: $("${PYTHON_CMD[@]}" --version 2>&1)"

  if ! has_cmd git; then
    if ((INSTALL_TOOLS)) && winget_available; then
      winget_install "Git.Git" "Git"
      append_known_windows_paths
    else
      die "Git mancante. Installa Git for Windows e ripeti."
    fi
  fi
  has_cmd git && log "Git OK: $(git --version)"

  if ((INSTALL_VCREDIST)) && winget_available; then
    winget_install "Microsoft.VCRedist.2015+.x64" "Microsoft Visual C++ Redistributable"
  fi

  if ((INSTALL_FFMPEG)) && winget_available; then
    winget_install "Gyan.FFmpeg" "FFmpeg"
  fi

  if ! find_poetry; then
    if ((INSTALL_TOOLS)); then
      install_poetry
    else
      die "Poetry mancante. Installa Poetry e ripeti."
    fi
  fi
  find_poetry || die "Poetry non trovato dopo installazione. Apri nuovo terminale e ripeti."
  log "Poetry OK: $("${POETRY_CMD[@]}" --version 2>&1)"
}

install_project_deps() {
  log "Installo dipendenze Python con Poetry (incluso waitress per Windows)"
  "${POETRY_CMD[@]}" install --with windows

  log "Verifico import principali"
  "${POETRY_CMD[@]}" run python -c "import cv2, flask, dotenv, cryptography, waitress; print('OK')"
}

fetch_detection_model() {
  log "Scarico modello classificazione (MobileNet-SSD)"
  "${POETRY_CMD[@]}" run python scripts/fetch_model.py
}

apply_mini_pc_tune() {
  if [[ ! -f ".env" ]]; then
    warn ".env mancante: salto tuning mini PC"
    return
  fi
  log "Applico profilo tuning mini-pc-windows"
  "${POETRY_CMD[@]}" run python scripts/env_profiles.py --profile mini-pc-windows
}

check_prerequisites() {
  log "Verifico prerequisiti runtime"
  "${POETRY_CMD[@]}" run python scripts/check_prerequisites.py || true
}

run_env_setup_if_needed() {
  if ((SETUP_ENV)); then
    log "Eseguo setup .env interattivo"
    if ((MINIMAL_ENV)); then
      "${POETRY_CMD[@]}" run python scripts/setup_config.py --minimal
    else
      "${POETRY_CMD[@]}" run python scripts/setup_config.py
    fi
    return
  fi

  if [[ ! -f ".env" ]]; then
    warn ".env mancante. Esegui: scripts/bootstrap_windows.sh --setup-env"
  else
    log ".env presente"
  fi
}

write_launcher() {
  local project_win
  project_win="$(win_path "$PROJECT_DIR")"

  log "Creo launcher Windows start_blackframe.bat"
  cat > "$PROJECT_DIR/start_blackframe.bat" <<BAT
@echo off
cd /d "$project_win"
poetry run python scripts\\check_prerequisites.py
if errorlevel 1 (
  echo.
  echo [BLACKFRAME] Prerequisiti mancanti. Vedi messaggi sopra.
  echo Installa ffmpeg: winget install Gyan.FFmpeg
  echo Poi riapri il terminale e rilancia.
  pause
  exit /b 1
)
poetry run python deploy\\serve_waitress.py >> "$project_win\\blackframe.log" 2>&1
BAT
}

open_firewall_port() {
  log "Configuro firewall Windows per porta TCP $APP_PORT"
  run_ps "\$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); if (-not \$isAdmin) { Write-Error 'Firewall richiede terminale come amministratore'; exit 1 }; \$name = 'BLACKFRAME'; if (-not (Get-NetFirewallRule -DisplayName \$name -ErrorAction SilentlyContinue)) { New-NetFirewallRule -DisplayName \$name -Direction Inbound -Protocol TCP -LocalPort $APP_PORT -Action Allow -Profile Private | Out-Null }; Write-Host 'Firewall OK'"
}

print_next_steps() {
  local project_win
  project_win="$(win_path "$PROJECT_DIR")"

  cat <<EOF

[$APP_NAME] Pronto.

Cartella progetto:
  $project_win

Comandi utili:
  poetry run python scripts\\setup_config.py --minimal
  poetry run python scripts\\check_prerequisites.py
  poetry run python scripts\\env_profiles.py --profile mini-pc-windows
  poetry run python deploy\\serve_waitress.py
  make fetch-model

Launcher:
  $project_win\\start_blackframe.bat

Browser:
  http://127.0.0.1:$APP_PORT

Per LAN:
  1. imposta APP_BIND_HOST=0.0.0.0 in .env
  2. riavvia script con --firewall da terminale amministratore
EOF
}

main() {
  log "Bootstrap Windows in $PROJECT_DIR"

  if ((INSTALL_TOOLS)); then
    if ! winget_available; then
      warn "winget non trovato. Posso solo verificare tool già installati."
    fi
  fi

  ensure_tools

  if ((INSTALL_DEPS)); then
    install_project_deps
  fi

  if ((FETCH_MODEL)); then
    fetch_detection_model
  fi

  run_env_setup_if_needed

  if ((TUNE_MINI_PC)); then
    apply_mini_pc_tune
  elif [[ -f ".env" ]] && ! grep -q '^MOTION_SCALE_WIDTH=' .env 2>/dev/null; then
    apply_mini_pc_tune
  fi

  write_launcher
  check_prerequisites

  if ((OPEN_FIREWALL)); then
    open_firewall_port
  fi

  print_next_steps

  if ((RUN_APP)); then
    [[ -f ".env" ]] || die ".env mancante: esegui prima con --setup-env"
    log "Avvio app. Ctrl+C per fermare."
    exec "${POETRY_CMD[@]}" run python deploy/serve_waitress.py
  fi
}

main "$@"
