# Guida di installazione — BLACKFRAME su Mini PC Windows

Questa guida copre l'installazione completa di BLACKFRAME su un mini PC Windows (Windows 10/11),
inclusi tutti i prerequisiti, la configurazione iniziale e l'avvio automatico al boot.

---

## Indice

1. [Prerequisiti hardware e rete](#1-prerequisiti-hardware-e-rete)
2. [Installare Python 3.11](#2-installare-python-311)
3. [Installare Git](#3-installare-git)
4. [Installare Poetry](#4-installare-poetry)
5. [Scaricare il codice](#5-scaricare-il-codice)
6. [Installare le dipendenze Python](#6-installare-le-dipendenze-python)
7. [Configurare il file .env](#7-configurare-il-file-env)
8. [Primo avvio e verifica](#8-primo-avvio-e-verifica)
9. [Avvio automatico al boot (Task Scheduler)](#9-avvio-automatico-al-boot-task-scheduler)
10. [Accesso da altri dispositivi in LAN](#10-accesso-da-altri-dispositivi-in-lan)
11. [Aggiornare il progetto](#11-aggiornare-il-progetto)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisiti hardware e rete

- Mini PC con Windows 10 o 11 (64-bit)
- Collegato alla stessa rete LAN della telecamera Tapo via ethernet o Wi-Fi
- Accesso ad internet per scaricare i tool (solo durante l'installazione)

**Credenziali Tapo che ti servono prima di iniziare:**

- IP della telecamera (es. `192.168.1.50`) — lo trovi nell'app Tapo → dispositivo → impostazioni avanzate
- Username e password dell'account RTSP (creato nell'app Tapo → sezione "Avanzate" → "Gestione account camera")
- Se la camera supporta PTZ: username e password ONVIF (di solito identici all'account camera)

---

## 2. Installare Python 3.11+

> BLACKFRAME richiede Python **3.11 o superiore** (3.11, 3.12, 3.13 vanno tutti bene).
> Versioni precedenti (3.9, 3.10) non sono supportate.

1. Vai su [python.org/downloads](https://www.python.org/downloads/) e scarica l'ultima versione
   **Python 3.x** stabile (scegli "Windows installer (64-bit)").

2. Avvia l'installer. **Spunta le due opzioni in basso prima di premere "Install Now":**
   - ✅ `Add Python 3.11 to PATH`
   - ✅ `Install launcher for all users`

3. Verifica l'installazione aprendo **PowerShell** (tasto Start → cerca "PowerShell"):

   ```powershell
   python --version
   ```

   Deve rispondere: `Python 3.11.x` (o 3.12.x / 3.13.x)

---

## 3. Installare Git

Git serve per scaricare il codice e per ricevere aggiornamenti futuri.

1. Vai su [git-scm.com/download/win](https://git-scm.com/download/win) e scarica l'installer a 64-bit.

2. Durante l'installazione lascia tutte le opzioni di default, tranne questa (opzionale ma consigliata):
   - In "Adjusting your PATH environment": scegli **"Git from the command line and also from 3rd-party software"**

3. Verifica:

   ```powershell
   git --version
   ```

   Deve rispondere: `git version 2.x.x.windows.x`

---

## 4. Installare Poetry

Poetry gestisce le dipendenze Python in un ambiente virtuale isolato.

In PowerShell (come utente normale, **non** come amministratore):

```powershell
$tmp = [System.IO.Path]::GetTempFileName() + '.py'
Invoke-WebRequest -Uri https://install.python-poetry.org -OutFile $tmp -UseBasicParsing
python $tmp
Remove-Item $tmp
```

> Scaricare su file prima di eseguire è più sicuro del classico `| python -`: un errore
> di rete o un MITM non viene mai eseguito come codice.

Poi aggiungi Poetry al PATH per la sessione corrente:

```powershell
$env:Path += ";$env:APPDATA\Python\Scripts"
```

Per renderlo permanente (così non devi rifarlo ad ogni avvio di PowerShell):

```powershell
[Environment]::SetEnvironmentVariable(
    "Path",
    [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:APPDATA\Python\Scripts",
    "User"
)
```

Verifica (apri una **nuova** finestra di PowerShell):

```powershell
poetry --version
```

Deve rispondere: `Poetry (version 1.x.x)`

---

## 5. Scaricare il codice

Scegli una cartella dove installare BLACKFRAME, ad esempio `C:\blackframe`.

```powershell
cd C:\
git clone <URL-del-repository> blackframe
cd blackframe
```

> Se non hai l'URL del repository, chiedi a chi ti ha fornito il progetto. In alternativa copia
> la cartella del progetto direttamente sul mini PC (via USB o rete) e salta il comando `git clone`.

---

## 6. Installare le dipendenze Python

Dalla cartella `C:\blackframe`:

```powershell
poetry install --with windows
```

Poetry crea automaticamente un ambiente virtuale isolato e installa Flask, OpenCV, waitress
(il server WSGI per Windows) e tutte le altre librerie. Il primo avvio richiede qualche minuto.

Verifica che tutto sia andato a buon fine:

```powershell
poetry run python -c "import cv2, flask, waitress; print('OK')"
```

Deve stampare `OK`.

---

## 7. Configurare il file .env

Il file `.env` contiene tutte le credenziali e le impostazioni dell'app. Non viene mai
salvato nel repository — va creato manualmente.

### 7a. Setup guidato (consigliato)

BLACKFRAME include uno script di setup interattivo che guida passo passo e genera
automaticamente le chiavi di sicurezza:

```powershell
python scripts\setup_config.py
```

Lo script chiede i valori fondamentali uno per uno. Premi Invio per accettare il default
suggerito, oppure digita il tuo valore.

I valori **obbligatori** che lo script richiede:

| Campo | Descrizione |
|---|---|
| `APP_ADMIN_PASSWORD` | Password per accedere all'interfaccia web. Sceglila lunga e unica. |
| `APP_SECRET_KEY` | Chiave crittografica interna. Premi Invio: viene generata automaticamente. |
| `TAPO_HOST` | IP della telecamera (es. `192.168.1.50`) |
| `TAPO_USERNAME` | Username account RTSP della camera |
| `TAPO_PASSWORD` | Password account RTSP della camera |

### 7b. Setup manuale (alternativa)

Se preferisci configurare a mano, copia il file di esempio:

```powershell
copy .env.example .env
```

Poi aprilo con Notepad (o qualsiasi editor di testo) e modifica almeno queste righe:

```dotenv
APP_ADMIN_PASSWORD=scegli-una-password-lunga
APP_SECRET_KEY=stringa-casuale-lunga-almeno-32-caratteri

TAPO_HOST=192.168.1.50
TAPO_USERNAME=utente_rtsp
TAPO_PASSWORD=password_rtsp
TAPO_RTSP_PORT=554
TAPO_STREAM_PATH=stream1
```

Per generare una `APP_SECRET_KEY` casuale sicura:

```powershell
poetry run python -c "import secrets; print(secrets.token_hex(32))"
```

### 7c. Impostazioni facoltative importanti

**Notifiche Telegram** (si possono configurare anche dall'interfaccia web dopo l'avvio):

```dotenv
NOTIFY_TELEGRAM_ENABLED=true
NOTIFY_TELEGRAM_BOT_TOKEN=il-tuo-token
NOTIFY_TELEGRAM_CHAT_ID=il-tuo-chat-id
```

**Registrazione video clip per evento:**

```dotenv
RECORD_ENABLED=true
RECORD_MAX_WIDTH=1280
```

**Registrazione continua (DVR):**

```dotenv
CONTINUOUS_RECORD_ENABLED=true
CONTINUOUS_RECORD_RETAIN_HOURS=24
```

---

## 8. Primo avvio e verifica

Avvia l'app:

```powershell
cd C:\blackframe
poetry run python deploy\serve_waitress.py
```

Vedrai dei log nel terminale. Quando compare una riga del tipo:

```
* Running on http://127.0.0.1:8000
```

Apri il browser e vai su: **http://127.0.0.1:8000**

Accedi con:
- **Username:** `admin`
- **Password:** quella impostata in `APP_ADMIN_PASSWORD`

Controlla che lo stream video sia visibile e che la sezione "Rilevamento" sia attiva.

Per fermare l'app: premi `Ctrl+C` nel terminale.

---

## 9. Avvio automatico al boot (Task Scheduler)

Per far partire BLACKFRAME automaticamente quando il mini PC si accende, senza dover
aprire un terminale ogni volta.

### 9a. Crea lo script di avvio

Crea il file `C:\blackframe\start_blackframe.bat` con questo contenuto:

```bat
@echo off
cd /d C:\blackframe
poetry run python deploy\serve_waitress.py >> C:\blackframe\blackframe.log 2>&1
```

> Il log dell'app viene salvato in `C:\blackframe\blackframe.log`. Utile per diagnosticare
> problemi di avvio.

### 9b. Configura Task Scheduler

1. Apri il menu Start, cerca **"Utilità di pianificazione"** (o "Task Scheduler") e aprila.

2. Nel pannello di destra, clicca **"Crea attività di base..."**

3. Compila la procedura guidata:

   - **Nome:** `BLACKFRAME`
   - **Trigger:** scegli **"All'avvio del computer"**
   - **Azione:** scegli **"Avvio programma"**
   - **Programma/Script:** `C:\blackframe\start_blackframe.bat`
   - **Inizia in:** `C:\blackframe`

4. Prima di cliccare Fine, spunta **"Apri la finestra di dialogo Proprietà al termine"** e clicca Fine.

5. Nella finestra Proprietà che si apre:
   - Scheda **"Generale"**: spunta **"Esegui che l'utente abbia o meno effettuato l'accesso"**
     e **"Esegui con i privilegi più elevati"**
   - Scheda **"Impostazioni"**: deseleziona "Interrompi l'attività se è in esecuzione da più di..."

6. Clicca OK e inserisci la password del tuo utente Windows se richiesta.

### 9c. Test del Task Scheduler

Per verificare che l'attività funzioni senza riavviare il PC:

1. Nella lista delle attività, tasto destro su **BLACKFRAME** → **"Esegui"**
2. Attendi 5 secondi, poi apri **http://127.0.0.1:8000** nel browser

Per vedere se l'app è in esecuzione:

```powershell
netstat -ano | findstr :8000
```

Deve mostrare una riga con `LISTENING`.

---

## 10. Accesso da altri dispositivi in LAN

Di default l'app risponde solo su `127.0.0.1` (solo dal mini PC stesso). Per accedere
dall'app Telegram, dal telefono, o da altri computer nella stessa rete:

### 10a. Abilita l'ascolto su tutta la rete

Nel file `.env` cambia:

```dotenv
APP_BIND_HOST=0.0.0.0
```

Riavvia l'app. Ora è raggiungibile da qualsiasi dispositivo in LAN.

### 10b. Apri la porta nel Firewall di Windows

1. Start → cerca **"Windows Defender Firewall"** → clicca **"Impostazioni avanzate"**
2. Nel pannello sinistro: **"Regole connessioni in entrata"** → **"Nuova regola..."**
3. Tipo: **Porta** → Avanti
4. TCP, porta specifica: `8000` → Avanti
5. **Consenti la connessione** → Avanti
6. Spunta solo **Privata** (rete domestica) → Avanti
7. Nome: `BLACKFRAME` → Fine

### 10c. Trova l'IP del mini PC

```powershell
ipconfig
```

Cerca la riga `Indirizzo IPv4` sotto l'adattatore di rete attivo (es. `192.168.1.100`).

Da qualsiasi altro dispositivo della stessa rete, apri: **http://192.168.1.100:8000**

> **Suggerimento:** Assegna un IP fisso al mini PC dal pannello di amministrazione del router
> (DHCP reservation) per evitare che l'IP cambi dopo un riavvio.

---

## 11. Aggiornare il progetto

Quando è disponibile una nuova versione:

```powershell
cd C:\blackframe
git pull
poetry install
```

Poi riavvia l'app (o riavvia il PC se usi Task Scheduler).

---

## 12. Troubleshooting

### `poetry: comando non trovato` / `poetry non riconosciuto`

Poetry è installato ma non è nel PATH della sessione corrente. Apri una **nuova** finestra
di PowerShell e riprova. Se il problema persiste, esegui di nuovo il comando per aggiungere
Poetry al PATH permanente (§4) e riapri PowerShell.

### La pagina web non si apre / "Impossibile raggiungere il sito"

1. Controlla che l'app sia in esecuzione: `netstat -ano | findstr :8000`
2. Se non è in esecuzione, apri `C:\blackframe\blackframe.log` per vedere l'errore
3. Se è in esecuzione ma non risponde da altri dispositivi, verifica che `APP_BIND_HOST=0.0.0.0`
   e che la regola del firewall sia attiva

### Lo stream video è nero o non si connette

- Verifica che il mini PC veda la camera: `ping 192.168.1.50`
- Controlla che `TAPO_HOST`, `TAPO_USERNAME`, `TAPO_PASSWORD` nel `.env` siano corretti
- Nell'app Tapo, verifica che l'account RTSP sia attivo (sezione "Avanzate" → account camera)
- Prova l'URL RTSP direttamente con VLC:
  `rtsp://utente:password@192.168.1.50:554/stream1`

### `poetry install --with windows` fallisce con errore su OpenCV

OpenCV richiede il runtime Visual C++. Se l'installazione fallisce:

1. Installa [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)
2. Riapri PowerShell e ripeti `poetry install --with windows`

### Il log mostra `Configura APP_ADMIN_PASSWORD prima di usare BLACKFRAME`

Il file `.env` è mancante o manca la variabile `APP_ADMIN_PASSWORD`. Riesegui il setup:

```powershell
python scripts\setup_config.py
```

### Le notifiche Telegram non arrivano

1. Verifica che `NOTIFY_TELEGRAM_ENABLED=true` nel `.env`
2. Nell'interfaccia web, sezione "Rilevamento" → pulsante **"Configura Telegram"**:
   usa il wizard per verificare token e chat ID
3. Verifica che il mini PC abbia accesso a internet (le notifiche usano l'API Telegram)

---

## Riferimenti rapidi

| Operazione | Comando |
|---|---|
| Avvio | `poetry run python deploy\serve_waitress.py` |
| Test suite | `poetry run python -m pytest -v` |
| Genera hash password | `poetry run python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; pw=getpass(); print(generate_password_hash(pw))"` |
| Log Task Scheduler | `C:\blackframe\blackframe.log` |
