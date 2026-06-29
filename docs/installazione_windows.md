# Guida installazione — BLACKFRAME su Mini PC Windows

Installazione **guidata da zero** per Windows 10/11 (64-bit). Il percorso consigliato è
un unico wizard che installa prerequisiti, configura `.env` ottimizzato per mini PC e
registra il servizio sempre attivo.

---

## Percorso rapido (consigliato)

### 1. Prepara il mini PC

- Collegato alla stessa LAN della telecamera Tapo (ethernet consigliato)
- Credenziali pronte:
  - **IP camera** (app Tapo → dispositivo → impostazioni avanzate)
  - **Account RTSP** (app Tapo → Avanzate → Gestione account camera)
  - **Password** per l'interfaccia web BLACKFRAME

### 2. Avvia il wizard

Apri **PowerShell** nella cartella del progetto (per il servizio NSSM usa **Esegui come amministratore**):

```powershell
cd C:\Users\nikom\smart-cam-manager
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_windows.ps1
```

Oppure doppio clic su `install-windows.bat`, oppure:

```powershell
.\blackframe.ps1 install-windows
```

> Su Windows **non serve `make`**. Usa `blackframe.ps1` o i comandi `poetry` indicati sotto.

### 3. Segui gli 8 passi

| Passo | Cosa fa |
|------|---------|
| 1 | Installa Python 3.11+, Git, FFmpeg, VC++ (via winget) |
| 2 | `poetry install --with windows` |
| 3 | Genera `.env.windows-minipc.example` (tuning ottimizzato) |
| 4 | Scarica modello classificazione (~180 MB) |
| 5 | Wizard `.env`: admin, camera, LAN, Telegram opzionale |
| 6 | Verifica prerequisiti |
| 7 | Servizio sempre attivo + firewall (se LAN) |
| 8 | Health check su `/health` |

Al termine apri **http://127.0.0.1:8000** (o la porta scelta).

---

## Template `.env` ottimizzato per mini PC

Il file **`.env.windows-minipc.example`** contiene già i valori di tuning consigliati:

| Parametro | Valore | Perché |
|-----------|--------|--------|
| `TAPO_STREAM_PATH` | `stream2` | Sottostream SD, meno CPU |
| `MOTION_SCALE_WIDTH` | `420` | Analisi MOG2 più leggera |
| `MOTION_THRESHOLD` | `42` | Meno falsi positivi |
| `RECORD_MAX_WIDTH` | `854` | Clip piccole, veloci da inviare |
| `CLASSIFICATION_ENABLED` | `true` | Persona/pet con MobileNet-SSD |
| `APP_ENABLE_OPEN_FOLDER` | `true` | Apri cartella clip da desktop locale |

**Copia rapida** (poi completa credenziali):

```powershell
copy .env.windows-minipc.example .env
notepad .env
```

Oppure rigenera il template:

```powershell
.\blackframe.ps1 env-example
```

Su Windows, `poetry run python scripts\setup_config.py` usa automaticamente
`.env.windows-minipc.example` come base se il file esiste.

---

## Servizio sempre attivo

Il wizard chiede come avviare l'app al boot:

### NSSM (consigliato)

- Servizio Windows vero
- **Avvio automatico al boot**
- **Riavvio automatico** se l'app crasha
- Log in `blackframe.log`

Il wizard scarica NSSM in `C:\Tools\nssm\` se mancante e registra il servizio `BLACKFRAME`.

**Comandi utili** (PowerShell):

```powershell
nssm status BLACKFRAME
nssm restart BLACKFRAME
nssm stop BLACKFRAME
sc query BLACKFRAME
```

### Task Scheduler (alternativa semplice)

- Avvio al boot tramite `start_blackframe.bat`
- **Nessun** riavvio automatico su crash
- Utile se non puoi usare NSSM

Configurazione manuale: vedi [appendice Task Scheduler](#appendice-task-scheduler).

### Solo manuale

Per test o debug:

```powershell
poetry run python deploy\serve_waitress.py
```

> **Importante:** non avviare manualmente l'app se il servizio NSSM è già attivo —
> avresti **due processi** sulla stessa porta. Verifica con:
> `netstat -ano | findstr ":8000.*LISTENING"`

---

## Accesso da telefono / altri PC in LAN

Durante il wizard rispondi **sì** a "Accesso da LAN". Imposta:

- `APP_BIND_HOST=0.0.0.0`
- Regola firewall sulla porta scelta (il wizard la crea se esegui come admin)

Trova l'IP del mini PC:

```powershell
ipconfig
```

Da un altro dispositivo: **http://192.168.x.x:8000**

Assegna un IP fisso (DHCP reservation sul router) per evitare che cambi dopo reboot.

---

## Opzioni avanzate del wizard

```powershell
# Solo servizio NSSM (configurazione già presente)
.\scripts\install_windows.ps1 -SkipWizard -SkipTools -SkipDeps -SkipModel -SkipConfig -ServiceMode nssm

# Salta registrazione servizio
.\scripts\install_windows.ps1 -SkipService

# Rigenera .env da zero
.\scripts\install_windows.ps1 -ForceConfig

# Forza Task Scheduler invece di NSSM
.\scripts\install_windows.ps1 -ServiceMode task

# Apri firewall (richiede admin)
.\scripts\install_windows.ps1 -SkipWizard -SkipTools -SkipDeps -SkipModel -SkipConfig -OpenFirewall
```

---

## Aggiornare il progetto

```powershell
nssm stop BLACKFRAME
cd C:\Users\nikom\smart-cam-manager
git pull
poetry install --with windows
poetry run python scripts\check_prerequisites.py
nssm start BLACKFRAME
```

`.env`, `data/` e `captures/` non vengono toccati.

---

## Troubleshooting

### Due processi sulla porta 8000

```powershell
netstat -ano | findstr ":8000.*LISTENING"
nssm stop BLACKFRAME
# termina eventuali PID python manuali
nssm start BLACKFRAME
```

### Clip non riproducibili nel browser

1. `ffmpeg -version` — se manca: `winget install Gyan.FFmpeg`, riapri PowerShell
2. `poetry run python scripts\check_prerequisites.py`

### Falsi eventi di movimento

Il profilo mini PC è già applicato. Se serve, alza `MOTION_THRESHOLD` dall'interfaccia web.

### Servizio non parte

```powershell
Get-Content C:\Users\nikom\smart-cam-manager\blackframe.log -Tail 80
poetry run python scripts\windows_service.py status
```

### Pagina non raggiungibile da LAN

- `APP_BIND_HOST=0.0.0.0` nel `.env`
- Regola firewall attiva
- `nssm restart BLACKFRAME`

---

## Appendice — installazione manuale passo-passo

Se preferisci capire ogni componente senza wizard:

1. **Python 3.11+** — [python.org/downloads](https://www.python.org/downloads/) (spunta "Add to PATH")
2. **Git** — [git-scm.com/download/win](https://git-scm.com/download/win)
3. **FFmpeg** — `winget install Gyan.FFmpeg`
4. **Poetry** — `https://install.python-poetry.org`
5. **Dipendenze** — `poetry install --with windows`
6. **Modello** — `.\blackframe.ps1 fetch-model`
7. **Config** — `copy .env.windows-minipc.example .env` + wizard:
   `poetry run python scripts\windows_wizard.py`
8. **Avvio** — servizio NSSM o `deploy\serve_waitress.py`

---

## Appendice — Task Scheduler

1. Usa `start_blackframe.bat` (generato dal wizard)
2. Apri **Utilità di pianificazione** → Crea attività di base
3. Nome: `BLACKFRAME`, trigger: **All'avvio del computer**
4. Azione: `C:\...\start_blackframe.bat`, cartella: root progetto
5. Proprietà → Generale: **Esegui che l'utente abbia o meno effettuato l'accesso**

Oppure da PowerShell:

```powershell
poetry run python scripts\windows_service.py install-task
```

---

## Riferimenti rapidi

| Operazione | Comando |
|------------|---------|
| **Crea installer .exe** | `.\blackframe.ps1 build-installer` |
| Guida installer | `docs\installer_windows.md` |
| Wizard completo | `.\install-windows.bat` oppure `.\blackframe.ps1 install-windows` |
| Template mini PC | `copy .env.windows-minipc.example .env` |
| Rigenera template | `.\blackframe.ps1 env-example` |
| Installa dipendenze | `.\blackframe.ps1 install` |
| Verifica prerequisiti | `.\blackframe.ps1 check-prerequisites` |
| Avvio produzione | `.\blackframe.ps1 serve` |
| Tutti i comandi | `.\blackframe.ps1 help` |
| Stato servizio | `poetry run python scripts\windows_service.py status` |
| Health check | `poetry run python scripts\windows_service.py health` |
| Log | `blackframe.log` nella root progetto |
| Gestione servizio | `docs/gestione_servizio.md` |
