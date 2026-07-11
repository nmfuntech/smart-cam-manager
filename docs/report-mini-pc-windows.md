# Report: ottimizzazione BLACKFRAME su mini PC Windows

Documento di sintesi delle modifiche in working area volte a far girare il sistema su hardware limitato (Windows): meno CPU, clip più leggere, registrazione/playback affidabile, notifiche Telegram coerenti con l’archivio UI.

**Scope working area:** 17 file modificati (+5 nuovi), circa **+1328 / −253** righe.

---

## 1. Configurazione (`.env` e profili)

Queste modifiche **non cambiano il codice di runtime**: regolano carico CPU, sensibilità motion, qualità clip e notifiche.

### 1.1 Profilo `mini-pc-windows` (`scripts/env_profiles.py`)

Comando:

```bash
poetry run python scripts/env_profiles.py --profile mini-pc-windows
```

oppure:

```bash
make install-windows
```

(flag `-TuneMiniPc` nello script PowerShell).

| Area | Parametro | Valore profilo | Perché |
|------|-----------|----------------|--------|
| **Stream** | `TAPO_STREAM_PATH` | `stream2` | Sottostream SD: meno banda e CPU rispetto a `stream1` HD |
| **Motion MOG2** | `MOTION_THRESHOLD` | 42 | Bilanciamento sensibilità/rumore su stream SD |
| | `MOTION_MIN_AREA` | 1200 | Evita micro-movimenti senza perdere soggetti piccoli |
| | `MOTION_SCALE_WIDTH` | 420 | Analisi su frame ridotto → meno CPU |
| | `MOTION_WARMUP_FRAMES` / `TRIGGER` / `CLEAR` | 20 / 2 / 10 | Stabilizzazione background e chiusura eventi |
| | `MOTION_GLOBAL_CHANGE_RATIO` | 0.4 | Ignora cambi luce globali (tramonto, nuvole) |
| | `MOTION_MOG2_HISTORY` / `MORPH_*` | 500 / 3 / 2 | MOG2 + morfologia più adatti a scene esterne |
| | `MOTION_EVENT_GAP` / `MAX_DURATION` | 5s / 30s | Eventi distinti senza clip infinite |
| **Registrazione eventi** | `RECORD_ENABLED` | true | Clip per ogni movimento |
| | `RECORD_FPS` / `MAX_WIDTH` | 6 / 854 | Clip leggere (~480p), adatte a mini PC e Telegram |
| | `RECORD_MAX_DURATION_SEC` | 22 | Limite durata per non saturare disco/CPU |
| | `RECORD_PREROLL` / `POSTROLL` | 2s / 2s | Contesto prima/dopo il movimento |
| **Classificazione** | `CLASSIFICATION_BACKEND` | `detection` | MobileNet-SSD locale (no cloud) |
| | `CLASSIFICATION_MIN_CONFIDENCE` | 0.58 | Soglia persona/pet |
| | `CLASSIFICATION_CROP_TO_MOTION` | false | Su Windows, crop disattivato → meno errori e CPU stabile |
| | `CLASSIFICATION_PET_PRIORITY_MARGIN` | 0.12 | Se persona e pet sono vicini, preferisce l’animale |
| **Telegram** | `NOTIFY_MIN_INTERVAL_SEC` | 6 | Anti-spam ma non perde eventi ravvicinati |
| | `NOTIFY_TELEGRAM_MAX_VIDEO_MB` | 20 | Oltre questa soglia invia foto invece del video |
| | `NOTIFY_PREFER_VIDEO` | true | Clip invece di snapshot |
| **UI** | `APP_ENABLE_OPEN_FOLDER` | true | Apre cartelle capture da interfaccia |

### 1.2 `.env` operativo (esempio installazione)

Allineato al profilo su: `stream2`, registrazione leggera, classificazione detection, Telegram, MOG2 avanzato.

Possibili differenze manuali rispetto al profilo:

- `MOTION_MIN_AREA=2200` (profilo: 1200) → leggermente **meno sensibile**
- `MOTION_THRESHOLD=48` (profilo: 42) → leggermente **più conservativo**

Non presenti (default = spento):

- `CONTINUOUS_RECORD_ENABLED` → registrazione continua **non attiva**
- `ffmpeg` non è una variabile env, ma è **prerequisito** per clip H.264 nel browser

### 1.3 Strumenti di configurazione aggiunti

| Strumento | Ruolo |
|-----------|--------|
| `scripts/env_profiles.py` | Applica tuning al `.env` senza toccare credenziali |
| `scripts/check_prerequisites.py` + `make check-prerequisites` | Verifica ffmpeg, modelli detection, tuning sospetto |
| `scripts/setup_config.py` | Su Windows default `stream2`; sezioni motion/record/classificazione estese; applica profilo piattaforma |
| `scripts/install_windows.ps1` + `make install-windows` | Installazione guidata (winget: Python, Poetry, ffmpeg) + tuning |
| `start_blackframe.bat` | Avvio produzione con check prerequisiti + log su `blackframe.log` |
| `.env.example` | Documentazione valori consigliati mini PC |

### 1.4 Prerequisito esterno critico (non è codice)

```powershell
winget install Gyan.FFmpeg
```

Su Windows OpenCV **non** produce H.264 nativamente (DLL OpenH264 rotta). Senza `ffmpeg` in PATH:

- le clip restano in codec `mp4v`
- non si riproducono nel browser
- la transcodifica post-registrazione non parte

---

## 2. Modifiche al software

### 2.1 Piattaforma Windows

| File | Cosa fa |
|------|---------|
| `recording.py` | Su `win32` usa solo `mp4v`; transcodifica H.264 via `ffmpeg` in `finalize_recording(transcode=True)` |
| `deploy/serve_waitress.py` | Aggiunge la root del repo a `sys.path` (fix `ModuleNotFoundError: app`) |
| `start_blackframe.bat` | Wrapper avvio + log |
| `docs/installazione_windows.md` | Guida installazione/operatività Windows ampliata |
| `docs/sviluppo_remoto_cursor_ssh.md` | Cursor Remote SSH dal Mac/altro PC verso mini PC |
| `docs/accesso_lan_minipc.md` | UI web in LAN dal browser |
| `CLAUDE.md` | Documentato gotcha OpenH264 + ffmpeg |

### 2.2 Pipeline registrazione eventi (`recording.py`, `app.py`, `CameraStream`)

- **Avvio anticipato clip**: registrazione MP4 al **primo** frame di motion (non solo dopo `trigger_frames`) → cattura gesti lenti, più pre-roll utile.
- **`open_event()`** in `motion_events.py`: crea la cartella evento prima del primo JPEG.
- **Preroll con sequence number**: il buffer RTSP traccia `(timestamp, jpeg, sequence)` → niente frame duplicati nel video.
- **`RECORD_POSTROLL_SEC`**: secondi di coda dopo la fine del movimento.
- **Callback registrazione sempre eseguito**: anche se `event.mp4` manca, si procede con notifica/fallback (prima il callback non partiva).

### 2.3 Notifiche Telegram (`notifications.py`, `app.py`)

| Problema risolto | Soluzione |
|------------------|-----------|
| Seconda notifica persa per `NOTIFY_MIN_INTERVAL` | Coda asincrona con worker (prima veniva **scartata**) |
| Telegram prima che l’evento sia in UI | Notifica **solo a evento chiuso** (`.closed`) |
| Screenshot invece del video | Dopo rinomina `__persona`/`__movimento`, il clip si risolve dalla **cartella rinominata** |
| Un evento in UI, zero Telegram (`no_detection`) | Anche “solo movimento” genera alert (esclusi `ignored` / `low_confidence`) |
| `notified` perso dopo rename | `find_event_dir()` + `on_delivered` solo a invio riuscito |
| Clip troppo grande per Telegram | Fallback automatico a `cover.jpg` sopra `NOTIFY_TELEGRAM_MAX_VIDEO_MB` |

### 2.4 Archivio eventi (`motion_events.py`, `routes/motion.py`)

- Suffisso categoria nelle cartelle: `motion_event_…__persona|movimento|animale_domestico`
- `find_event_dir()`: risolve path dopo rename (meta, dedup, notifiche)
- Pattern URL API aggiornato per servire eventi con suffisso

### 2.5 Classificazione (`classification.py`)

- Logica **persona vs pet** con margine `CLASSIFICATION_PET_PRIORITY_MARGIN` (evita “cane classificato come persona”)
- Selezione candidati multipli invece del solo score massimo

### 2.6 Test e qualità

- Nuovi test: notifiche a chiusura evento, video post-rename, dedup su disco, profili env
- Test esistenti aggiornati per il nuovo comportamento Telegram/registrazione

### 2.7 File toccati (riferimento git)

**Modificati:**

- `.env.example`, `CLAUDE.md`, `Makefile`
- `app.py`, `classification.py`, `motion_events.py`, `notifications.py`, `recording.py`
- `deploy/serve_waitress.py`, `routes/motion.py`
- `docs/installazione_windows.md`
- `scripts/bootstrap_windows.sh`, `scripts/install_ubuntu.sh`, `scripts/setup_config.py`
- `tests/test_app.py`, `tests/test_classification.py`, `tests/test_features.py`

**Nuovi:**

- `scripts/check_prerequisites.py`
- `scripts/env_profiles.py`
- `scripts/install_windows.ps1`
- `start_blackframe.bat`
- `tests/test_env_profiles.py`

---

## 3. Mappa effetto → sintomo

| Sintomo | Causa | Risolto da |
|---------|-------|------------|
| CPU alta / falsi positivi | Stream HD + motion aggressivo | `.env`: `stream2`, MOG2 tuning |
| Clip non riproducibili in browser | OpenH264 rotto su Windows | Software: `mp4v` + `ffmpeg` transcode |
| Telegram ≠ archivio UI | Notifica prima della chiusura / dedup errato | Software: flusso unificato a chiusura evento |
| Screenshot invece di video | Path clip invalido dopo rename | Software: `_deliver_event_notification` |
| 1 notifica su 2 eventi | Coda scartata + `no_detection` silenziato | Software: worker coda + notify su movimento |
| App non parte da `serve_waitress` | `sys.path` errato | Software: fix `deploy/serve_waitress.py` |

---

## 4. Registrazione continua (stato attuale)

Il codice per la registrazione continua (`ContinuousRecorder`) è presente, ma con `CONTINUOUS_RECORD_ENABLED=false` (default) **non registra nulla**.

Quando attiva, salva in:

```text
captures/continuous/<camera_id>/segment_YYYYMMDD_HHMMSS.mp4
```

Esempio con profilo `env-default`:

```text
captures/continuous/env-default/segment_20260624_120000.mp4
```

Parametri principali:

| Parametro | Default | Effetto |
|-----------|---------|---------|
| `CONTINUOUS_RECORD_SEGMENT_MIN` | 10 | Durata di ogni file (minuti) |
| `CONTINUOUS_RECORD_RETAIN_HOURS` | 3 | Quanto tenere in totale (rotazione automatica) |
| `RECORD_FPS` / `RECORD_MAX_WIDTH` | come eventi | Stessi parametri delle clip motion |

Attivazione:

```env
CONTINUOUS_RECORD_ENABLED=true
CONTINUOUS_RECORD_SEGMENT_MIN=10
CONTINUOUS_RECORD_RETAIN_HOURS=24
```

La registrazione continua resta disattivata nel profilo mini-PC: aggiunge resize,
encoding e scrittura disco costanti. Attivarla solo quando l'archivio 24/7 è un
requisito più importante del budget CPU/RAM.

oppure via Telegram: `/continuous_on`.

**Nota:** i segmenti continui restano in `mp4v` (no transcodifica massiva); per l’archivio locale vanno bene, nel browser potrebbero non riprodursi.

---

## 5. Cosa resta operativo da sapere

- **Riavvio consigliato** dopo modifiche `.env` o al codice notifiche/registrazione.
- **Permessi file Windows:** alcuni test su `chmod 0o600` falliscono su Windows (comportamento noto, non blocca l’uso).
- **Cartelle capture eventi:** `captures/motion/<profile_id>/` (es. `captures/motion/env-default/`).

---

## 6. Comandi utili

```powershell
make check-prerequisites          # ffmpeg, modelli, tuning
make run                          # dev
.\start_blackframe.bat            # produzione Windows + log
poetry run python scripts/env_profiles.py --profile mini-pc-windows
```

---

## 7. Sintesi

| Layer | Cosa fa |
|-------|---------|
| **Tuning `.env`** | Alleggerisce stream, motion e clip per il mini PC |
| **Software** | Registrazione H.264 affidabile su Windows, allineamento Telegram/archivio, fix path video e coda notifiche |
