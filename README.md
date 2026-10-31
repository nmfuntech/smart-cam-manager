# BLACKFRAME

Videosorveglianza self-hosted per telecamere Tapo (RTSP/ONVIF): rilevamento
del movimento, riconoscimento di persone e animali, clip degli eventi e
gestione completa da **Telegram**. Gira su hardware modesto (in produzione un
mini PC Intel N95 con Windows 11), senza cloud e senza database.

## Cosa fa

- **Rilevamento** del movimento con sottrazione dello sfondo, **zone di
  esclusione** disegnate sull'immagine e riconoscimento persone/animali con
  YOLOX-Tiny ([dettagli e benchmark](docs/rilevamento.md)).
- **Avvisi Telegram** con foto o clip, bottoni rapidi (📸 Live, 🎬 Clip,
  🔕 Pausa 1h, ❌ Falso allarme), orari di quiete che non silenziano mai le
  persone, pausa che sopravvive ai riavvii.
- **Modalità** armato / casa / disarmato, manuali o programmate
  (`MODE_SCHEDULE`).
- **Salute del sistema:** avvisi se la camera va offline e quando torna, se il
  rilevamento si blocca, se il disco si riempie; messaggio a ogni avvio;
  riepilogo giornaliero; ping verso un servizio esterno (`HEARTBEAT_URL`) per
  sapere se cade l'intero mini PC.
- **Accessi:** inviti Telegram monouso con scadenza e ruoli (`famiglia`,
  `stato` senza immagini), registro delle azioni (`/log`).
- **Domotica:** regole che accendono dispositivi Tuya in LAN sugli eventi.
- **Agente in linguaggio naturale** (opzionale, Ollama locale) che traduce
  frasi libere in comandi, con conferma per quelli che cambiano lo stato.
- **Interfaccia web** per live, eventi, impostazioni e zone.

## Avvio rapido

Requisiti: Python 3.11+, [Poetry](https://python-poetry.org/), ffmpeg.

```bash
make install        # dipendenze
make setup          # .env guidato (credenziali camera, password admin, chiavi)
make fetch-model    # modello YOLOX-Tiny per il riconoscimento
make run            # http://localhost:8000
```

Su Windows si usa `.\blackframe.ps1` al posto di `make`
([installazione Windows](docs/installazione_windows.md)).

## Comandi Telegram principali

| Comando | Cosa fa |
|---|---|
| `/status` | Stato di camera, rilevamento, modalità e avvisi aperti |
| `/snapshot`, `/clip 10` | Foto live, clip live di N secondi |
| `/arma`, `/casa`, `/disarma`, `/modalita` | Modalità di sicurezza |
| `/mute 30`, `/resume` | Pausa e ripresa degli avvisi |
| `/events`, `/latest` | Ultimi eventi |
| `/invite [stato]`, `/guests`, `/revoke`, `/log` | Accessi e registro (admin) |
| `/menu` | Tutti i bottoni |

## Sviluppo

```bash
make test           # suite completa (ermetica: non tocca .env né data/)
make lint           # ruff
make deploy         # test, push e aggiornamento sicuro del mini PC
make deploy-status  # versione e stato del servizio sul mini PC
```

Il flusso Mac → mini PC (test in worktree, riavvio, health check, rollback
automatico) è descritto in [docs/deploy.md](docs/deploy.md). La CI esegue i
test su Linux e Windows.

## Struttura

```
src/blackframe/
  app.py               composition root: costruisce i servizi e l'app Flask
  camera_stream.py     lettura RTSP con riconnessione e buffer pre-roll
  motion_detector.py   loop di movimento, ciclo di vita degli eventi, avvisi
  classification.py    backend di riconoscimento (YOLOX, SSD, cloud...)
  motion_zones.py      zone di esclusione
  health.py            watchdog, riepilogo giornaliero, heartbeat
  modes.py             modalità armato/casa/disarmato
  notifications.py     coda avvisi Telegram persistente con retry
  telegram_commands.py bot comandi (worker per chat, inviti, audit)
  telegram_client.py   unico client Bot API
  commands/            registry dei comandi condiviso da Telegram e agente
  agent/               agente NLU (Ollama)
  automation/          regole e dispositivi Tuya
  routes/              API e pagine web
config/                regole di automazione, profili prestazioni
scripts/               setup, fetch modelli, benchmark, deploy Windows
docs/                  guide
```

## Documentazione

- [Rilevamento e taratura](docs/rilevamento.md)
- [Deploy Mac → mini PC](docs/deploy.md)
- [Installazione Windows](docs/installazione_windows.md) ·
  [gestione servizio](docs/gestione_servizio.md) ·
  [accesso LAN](docs/accesso_lan_minipc.md)
- [Automazione Tuya](docs/automation_tuya_fase0.md)
- Tutte le variabili sono documentate in `.env.example`.
