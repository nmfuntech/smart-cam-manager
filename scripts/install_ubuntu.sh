#!/usr/bin/env bash
# Install BLACKFRAME on Ubuntu 22.04+ (server o desktop).
# Esegui dalla directory del progetto:
#   bash scripts/install_ubuntu.sh [opzioni]
#
# Con --install-service copia il progetto in /opt/blackframe e installa
# un'unit systemd che avvia l'app automaticamente all'avvio.

set -Eeuo pipefail

APP_NAME="BLACKFRAME"
APP_PORT="${APP_PORT:-8000}"

# flag
DO_SYSTEM_DEPS=1
DO_POETRY=1
DO_PROJECT_DEPS=1
DO_SETUP_ENV=0
MINIMAL_ENV=1
DO_INSTALL_SERVICE=0
DO_BF_SYMLINK=1
DO_RUN=0

INSTALL_DIR="/opt/blackframe"
SERVICE_USER="blackframe"
PYTHON_MIN_MINOR=11   # Python 3.x, x >= this

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

log()  { printf '\n[%s] %s\n' "$APP_NAME" "$*"; }
warn() { printf '\n[%s] WARNING: %s\n' "$APP_NAME" "$*" >&2; }
die()  { printf '\n[%s] ERROR: %s\n'   "$APP_NAME" "$*" >&2; exit 1; }

require_root() {
  [[ $EUID -eq 0 ]] || die "Questo step richiede i permessi di root. Riesegui con sudo."
}

has_cmd() { command -v "$1" &>/dev/null; }

usage() {
  cat <<'USAGE'
Uso:
  bash scripts/install_ubuntu.sh [opzioni]

Opzioni:
  --setup-env          esegui setup interattivo .env dopo l'installazione
  --full-env           con --setup-env, chiede anche valori non essenziali
  --install-service    copia progetto in /opt/blackframe e installa unit systemd
  --install-dir DIR    directory di destinazione (default: /opt/blackframe)
  --service-user USER  utente di sistema per il servizio (default: blackframe)
  --no-system-deps     non installa pacchetti apt
  --no-poetry          non installa/aggiorna Poetry
  --no-deps            non esegue poetry install
  --no-bf-symlink      non crea symlink bf in ~/.local/bin
  --run                avvia l'app alla fine (solo modalità locale, non servizio)
  --port PORT          porta app (default: 8000, env: APP_PORT)
  -h, --help           mostra questa guida

Esempi:
  # Sviluppo locale - installa dipendenze e configura .env
  bash scripts/install_ubuntu.sh --setup-env

  # Server - installa come servizio systemd con utente dedicato (richiede sudo)
  sudo bash scripts/install_ubuntu.sh --install-service --setup-env

  # Solo dipendenze Python, .env già configurato
  bash scripts/install_ubuntu.sh

USAGE
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

while (($#)); do
  case "$1" in
    --setup-env)        DO_SETUP_ENV=1 ;;
    --full-env)         MINIMAL_ENV=0 ;;
    --install-service)  DO_INSTALL_SERVICE=1 ;;
    --install-dir)      INSTALL_DIR="${2:?--install-dir richiede un valore}"; shift ;;
    --service-user)     SERVICE_USER="${2:?--service-user richiede un valore}"; shift ;;
    --no-system-deps)   DO_SYSTEM_DEPS=0 ;;
    --no-poetry)        DO_POETRY=0 ;;
    --no-deps)          DO_PROJECT_DEPS=0 ;;
    --no-bf-symlink)    DO_BF_SYMLINK=0 ;;
    --run)              DO_RUN=1 ;;
    --port)             APP_PORT="${2:?--port richiede un valore}"; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  die "Opzione sconosciuta: $1. Usa --help." ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# rilevamento sistema
# ---------------------------------------------------------------------------

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

detect_ubuntu() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    source /etc/os-release
    OS_NAME="${NAME:-}"
    OS_VERSION="${VERSION_ID:-}"
  else
    OS_NAME=""
    OS_VERSION=""
  fi
  if [[ "$OS_NAME" != *"Ubuntu"* ]]; then
    warn "Sistema rilevato: ${OS_NAME:-sconosciuto}. Lo script è testato su Ubuntu 22.04+."
    warn "Premi Invio per continuare comunque, oppure Ctrl+C per annullare."
    read -r
  fi
}

# ---------------------------------------------------------------------------
# Python 3.11+
# ---------------------------------------------------------------------------

find_python() {
  local py
  for py in python3.13 python3.12 python3.11; do
    if has_cmd "$py"; then
      local minor
      minor="$("$py" -c 'import sys; print(sys.version_info.minor)')"
      if (( minor >= PYTHON_MIN_MINOR )); then
        PYTHON_CMD="$py"
        return 0
      fi
    fi
  done
  # fallback: controlla python3 generico
  if has_cmd python3; then
    local minor
    minor="$(python3 -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
    if (( minor >= PYTHON_MIN_MINOR )); then
      PYTHON_CMD="python3"
      return 0
    fi
  fi
  return 1
}

install_python() {
  log "Python 3.${PYTHON_MIN_MINOR}+ non trovato — installo via deadsnakes PPA"
  require_root
  apt-get install -y software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -qq
  apt-get install -y "python3.11" "python3.11-venv" "python3.11-dev"
  PYTHON_CMD="python3.11"
}

# ---------------------------------------------------------------------------
# dipendenze di sistema
# ---------------------------------------------------------------------------

install_system_deps() {
  log "Installo dipendenze di sistema"
  require_root

  apt-get update -qq

  # runtime OpenCV headless: libGL e glib sono indispensabili per cv2
  local pkgs=(
    git
    ffmpeg
    curl
    libgl1
    libglib2.0-0
    libgomp1
    python3-pip
    python3-venv
  )

  apt-get install -y "${pkgs[@]}"
  log "Dipendenze di sistema installate"
}

# ---------------------------------------------------------------------------
# Poetry
# ---------------------------------------------------------------------------

find_poetry() {
  if has_cmd poetry; then
    POETRY_CMD="poetry"
    return 0
  fi
  local user_poetry="$HOME/.local/bin/poetry"
  if [[ -x "$user_poetry" ]]; then
    POETRY_CMD="$user_poetry"
    return 0
  fi
  return 1
}

install_poetry() {
  log "Installo Poetry"
  if ! has_cmd curl; then
    die "curl non trovato. Installa curl oppure usa --no-system-deps dopo aver installato curl manualmente."
  fi
  # Niente 'curl | python': scarica l'installer su file e poi eseguilo. Cosi' un
  # MITM o un errore HTTP (pagina di errore al posto dello script) non vengono
  # eseguiti come codice. TLS forzato + --fail per abortire sugli errori.
  local installer
  installer="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$installer'" RETURN
  if ! curl -fsSL --proto '=https' --tlsv1.2 \
        https://install.python-poetry.org -o "$installer"; then
    die "Download dell'installer Poetry fallito."
  fi
  # Verifica opzionale del checksum: esporta POETRY_INSTALLER_SHA256 per fissarlo
  # a un valore noto e bloccare l'esecuzione se l'installer cambia inaspettatamente.
  if [[ -n "${POETRY_INSTALLER_SHA256:-}" ]]; then
    echo "${POETRY_INSTALLER_SHA256}  ${installer}" | sha256sum -c - \
      || die "Checksum installer Poetry non corrispondente: installazione interrotta."
  fi
  "$PYTHON_CMD" "$installer"
  # aggiunge ~/.local/bin al PATH per questa sessione
  export PATH="$HOME/.local/bin:$PATH"
  find_poetry || die "Poetry non trovato dopo installazione. Apri un nuovo terminale e ripeti."
  log "Poetry OK: $("$POETRY_CMD" --version)"
}

configure_poetry() {
  # Usa un venv in-project (.venv/) per semplificare il path nel service file
  "$POETRY_CMD" config virtualenvs.in-project true --local
}

# ---------------------------------------------------------------------------
# dipendenze Python
# ---------------------------------------------------------------------------

install_project_deps() {
  log "Installo dipendenze Python con Poetry"
  cd "$PROJECT_DIR"
  "$POETRY_CMD" install --no-root

  log "Verifico import principali"
  "$POETRY_CMD" run python -c "
import cv2, flask, dotenv, cryptography, requests
print('cv2:', cv2.__version__)
print('flask: OK')
print('cryptography: OK')
print('requests: OK')
"
}

# ---------------------------------------------------------------------------
# configurazione .env
# ---------------------------------------------------------------------------

run_env_setup() {
  cd "$PROJECT_DIR"
  if ((DO_SETUP_ENV)); then
    log "Avvio setup .env interattivo"
    if ((MINIMAL_ENV)); then
      "$POETRY_CMD" run python scripts/setup_config.py --minimal
    else
      "$POETRY_CMD" run python scripts/setup_config.py
    fi
    return
  fi

  if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    warn ".env mancante. Esegui: bash scripts/install_ubuntu.sh --setup-env"
    warn "oppure: poetry run python scripts/setup_config.py --minimal"
  else
    log ".env presente"
  fi
}

# ---------------------------------------------------------------------------
# symlink CLI bf
# ---------------------------------------------------------------------------

install_bf_symlink() {
  local bin_dir="$HOME/.local/bin"
  mkdir -p "$bin_dir"
  local target="$bin_dir/bf"
  # wrapper che richiama poetry run dal project dir
  cat > "$target" <<SCRIPT
#!/usr/bin/env bash
exec "$POETRY_CMD" --directory "$PROJECT_DIR" run python "$PROJECT_DIR/scripts/bf.py" "\$@"
SCRIPT
  chmod +x "$target"
  log "Comando bf installato in $target"

  # suggerisci aggiunta PATH se non è già lì
  if [[ ":$PATH:" != *":$bin_dir:"* ]]; then
    warn "$bin_dir non è nel PATH."
    warn "Aggiungi questa riga a ~/.bashrc (o ~/.zshrc):"
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
}

# ---------------------------------------------------------------------------
# installazione servizio systemd
# ---------------------------------------------------------------------------

install_service() {
  require_root
  log "Installo servizio systemd (utente: $SERVICE_USER, dir: $INSTALL_DIR)"

  # crea utente di sistema dedicato se non esiste
  if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" \
      --create-home --comment "BLACKFRAME camera monitor" "$SERVICE_USER"
    log "Utente $SERVICE_USER creato"
  fi

  # copia file progetto
  log "Copio progetto in $INSTALL_DIR"
  rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='captures/' \
    --exclude='data/' \
    --exclude='.env' \
    --exclude='*.pyc' \
    "$PROJECT_DIR/" "$INSTALL_DIR/"

  # crea directory scrivibili
  mkdir -p "$INSTALL_DIR/captures/motion" "$INSTALL_DIR/data"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
  chmod 750 "$INSTALL_DIR"

  # installa dipendenze nel venv del servizio
  log "Installo dipendenze Python in $INSTALL_DIR"
  su -s /bin/bash "$SERVICE_USER" -c "
    cd '$INSTALL_DIR'
    '$POETRY_CMD' config virtualenvs.in-project true --local
    '$POETRY_CMD' install --no-root
  "

  # setup .env nel INSTALL_DIR se non esiste ancora
  if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    if [[ -f "$PROJECT_DIR/.env" ]]; then
      cp "$PROJECT_DIR/.env" "$INSTALL_DIR/.env"
      chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
      chmod 600 "$INSTALL_DIR/.env"
      log ".env copiato da $PROJECT_DIR/.env"
    else
      warn "$INSTALL_DIR/.env mancante."
      warn "Crea $INSTALL_DIR/.env prima di avviare il servizio:"
      warn "  sudo -u $SERVICE_USER poetry -C $INSTALL_DIR run python scripts/setup_config.py --minimal"
    fi
  fi

  # genera unit systemd a partire dal template, aggiornando User e WorkingDirectory
  local service_file="/etc/systemd/system/blackframe.service"
  sed \
    -e "s|User=blackframe|User=$SERVICE_USER|g" \
    -e "s|WorkingDirectory=/opt/blackframe|WorkingDirectory=$INSTALL_DIR|g" \
    -e "s|EnvironmentFile=/opt/blackframe/.env|EnvironmentFile=$INSTALL_DIR/.env|g" \
    -e "s|ExecStart=/opt/blackframe/.venv/bin/gunicorn|ExecStart=$INSTALL_DIR/.venv/bin/gunicorn|g" \
    -e "s|ReadWritePaths=/opt/blackframe/captures /opt/blackframe/data|ReadWritePaths=$INSTALL_DIR/captures $INSTALL_DIR/data|g" \
    "$INSTALL_DIR/deploy/blackframe.service" > "$service_file"
  chmod 644 "$service_file"

  systemctl daemon-reload
  systemctl enable blackframe.service
  log "Servizio blackframe abilitato (non ancora avviato)"
  log "Per avviarlo: sudo systemctl start blackframe"
}

# ---------------------------------------------------------------------------
# riepilogo finale
# ---------------------------------------------------------------------------

print_next_steps() {
  local venv_python="$PROJECT_DIR/.venv/bin/python"
  local bf_wrapper="$HOME/.local/bin/bf"

  cat <<EOF

[$APP_NAME] Installazione completata.

Directory progetto:  $PROJECT_DIR
Python usato:        $PYTHON_CMD ($("$PYTHON_CMD" --version 2>&1))
Poetry:              $("$POETRY_CMD" --version 2>&1)

EOF

  if ((DO_INSTALL_SERVICE)); then
    cat <<EOF
Servizio systemd:
  sudo systemctl start blackframe
  sudo systemctl status blackframe
  sudo journalctl -u blackframe -f

App installata in:   $INSTALL_DIR
Utente servizio:     $SERVICE_USER

EOF
  else
    cat <<EOF
Avvia l'app:
  make run                          (dev, Flask)
  make serve                        (prod, gunicorn)

EOF
  fi

  if ((DO_BF_SYMLINK)) && [[ -x "$bf_wrapper" ]]; then
    cat <<EOF
CLI bf installato:
  bf status
  bf events --limit 5
  bf config
  bf motion on|off
  bf classify on|off
  bf notify on|off

EOF
  fi

  if [[ ! -f "$PROJECT_DIR/.env" ]] && ! ((DO_INSTALL_SERVICE)); then
    cat <<EOF
Prossimo passo obbligatorio:
  bash scripts/install_ubuntu.sh --setup-env
oppure:
  poetry run python scripts/setup_config.py --minimal

EOF
  fi

  cat <<EOF
Accesso UI:  http://127.0.0.1:$APP_PORT
EOF
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
  detect_ubuntu
  log "Avvio installazione da $PROJECT_DIR"

  # dipendenze di sistema (apt)
  if ((DO_SYSTEM_DEPS)); then
    if [[ $EUID -eq 0 ]]; then
      install_system_deps
    else
      log "Non root: salto dipendenze apt (usa --no-system-deps per silenziare o riesegui con sudo)"
      warn "Pacchetti richiesti: git ffmpeg curl libgl1 libglib2.0-0 libgomp1 python3-venv"
    fi
  fi

  # Python
  if ! find_python; then
    install_python
  fi
  log "Python OK: $PYTHON_CMD ($("$PYTHON_CMD" --version 2>&1))"

  # Poetry
  if ((DO_POETRY)); then
    if ! find_poetry; then
      install_poetry
    else
      log "Poetry OK: $("$POETRY_CMD" --version 2>&1)"
    fi
  else
    find_poetry || die "Poetry non trovato e --no-poetry specificato."
  fi

  configure_poetry

  # dipendenze Python
  if ((DO_PROJECT_DEPS)); then
    install_project_deps
  fi

  # .env
  run_env_setup

  # CLI bf
  if ((DO_BF_SYMLINK)); then
    install_bf_symlink
  fi

  # servizio systemd
  if ((DO_INSTALL_SERVICE)); then
    install_service
  fi

  # avvio diretto (solo modalità locale)
  if ((DO_RUN)); then
    if ((DO_INSTALL_SERVICE)); then
      warn "--run ignorato con --install-service. Usa: sudo systemctl start blackframe"
    else
      [[ -f "$PROJECT_DIR/.env" ]] || die ".env mancante: esegui prima --setup-env"
      log "Avvio app. Ctrl+C per fermare."
      cd "$PROJECT_DIR"
      exec "$POETRY_CMD" run python app.py
    fi
  fi

  print_next_steps
}

main "$@"
