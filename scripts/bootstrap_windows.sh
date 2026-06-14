#!/usr/bin/env bash
set -Eeuo pipefail

# Bootstrap BLACKFRAME on Windows from Git Bash / MSYS / WSL with Windows interop.
# Installs/checks Python 3.11, Git, Poetry, Visual C++ runtime, project deps,
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
    --accept-source-agreements || warn "$label non installato via winget. Proseguo se gia presente."
}

append_known_windows_paths() {
  local candidates=()

  if [[ -n "${LOCALAPPDATA:-}" ]]; then
    candidates+=(
      "$LOCALAPPDATA\\Programs\\Python\\Python311"
      "$LOCALAPPDATA\\Programs\\Python\\Python311\\Scripts"
    )
  fi

  if [[ -n "${APPDATA:-}" ]]; then
    candidates+=("$APPDATA\\Python\\Scripts")
  fi

  candidates+=(
    "C:\\Program Files\\Python311"
    "C:\\Program Files\\Python311\\Scripts"
    "C:\\Program Files\\Git\\cmd"
  )

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

python_is_311() {
  "$@" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >/dev/null 2>&1
}

find_python311() {
  append_known_windows_paths

  if has_cmd py && python_is_311 py -3.11; then
    PYTHON_CMD=(py -3.11)
    return 0
  fi

  if has_cmd python && python_is_311 python; then
    PYTHON_CMD=(python)
    return 0
  fi

  if has_cmd python3 && python_is_311 python3; then
    PYTHON_CMD=(python3)
    return 0
  fi

  local candidates=()
  if [[ -n "${LOCALAPPDATA:-}" ]]; then
    candidates+=("$LOCALAPPDATA\\Programs\\Python\\Python311\\python.exe")
  fi
  candidates+=("C:\\Program Files\\Python311\\python.exe")

  local candidate unix_candidate
  for candidate in "${candidates[@]}"; do
    unix_candidate="$(unix_path "$candidate")"
    if [[ -x "$unix_candidate" ]] && python_is_311 "$unix_candidate"; then
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
  if has_cmd curl; then
    curl -sSL https://install.python-poetry.org | "${PYTHON_CMD[@]}" -
  else
    run_ps "(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | py -3.11 -"
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

  if ! find_python311; then
    if ((INSTALL_TOOLS)) && winget_available; then
      winget_install "Python.Python.3.11" "Python 3.11"
      append_known_windows_paths
    else
      die "Python 3.11 mancante. Installa Python 3.11 x64 e ripeti."
    fi
  fi
  find_python311 || die "Python 3.11 non trovato dopo installazione. Apri nuovo terminale e ripeti."
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
  log "Installo dipendenze Python con Poetry"
  "${POETRY_CMD[@]}" install

  log "Verifico import principali"
  "${POETRY_CMD[@]}" run python -c "import cv2, flask, dotenv, cryptography; print('OK')"
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
poetry run python app.py >> "$project_win\\blackframe.log" 2>&1
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
  poetry run python app.py

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
      warn "winget non trovato. Posso solo verificare tool gia installati."
    fi
  fi

  ensure_tools

  if ((INSTALL_DEPS)); then
    install_project_deps
  fi

  run_env_setup_if_needed
  write_launcher

  if ((OPEN_FIREWALL)); then
    open_firewall_port
  fi

  print_next_steps

  if ((RUN_APP)); then
    [[ -f ".env" ]] || die ".env mancante: esegui prima con --setup-env"
    log "Avvio app. Ctrl+C per fermare."
    exec "${POETRY_CMD[@]}" run python app.py
  fi
}

main "$@"
