# Funzionalità videosorveglianza casalinga

Guida alle funzionalità aggiunte per l'uso come sistema di videosorveglianza locale
24/7: notifiche, registrazione video, retention automatica e deploy come servizio.

## 1. Notifiche Telegram

Avviso push con foto (la `cover.jpg` dell'evento) quando viene rilevato un movimento.

Setup:
1. Crea un bot con [@BotFather](https://t.me/BotFather) e ottieni il **bot token**.
2. Scopri il tuo **chat id** (es. scrivendo al bot e leggendo
   `https://api.telegram.org/bot<token>/getUpdates`).
3. Imposta nel `.env`:

```
NOTIFY_TELEGRAM_ENABLED=true
NOTIFY_TELEGRAM_BOT_TOKEN=123456:ABC...
NOTIFY_TELEGRAM_CHAT_ID=987654321
NOTIFY_ON_CLASSES=persona          # opzionale, vuoto = tutti gli eventi
NOTIFY_MIN_INTERVAL_SEC=30         # anti-flood
```

Note:
- Il filtro `NOTIFY_ON_CLASSES` richiede `CLASSIFICATION_ENABLED=true` (serve l'etichetta
  della classe). Con la classificazione disattivata, lascia `NOTIFY_ON_CLASSES` vuoto.
- L'invio avviene in un thread separato: non rallenta mai il motion detection.
- Il toggle `NOTIFY_TELEGRAM_ENABLED` è anche nella UI (sidebar Controlli).

## 2. Registrazione video (clip MP4)

Ogni evento produce un `event.mp4` nella cartella dell'evento, con un breve pre-roll
catturato prima del trigger. La clip è riproducibile dal viewer (player video nella
preview) e via `GET /motion_event/<id>/video.mp4` (supporta seek con HTTP Range).

```
RECORD_ENABLED=true
RECORD_FPS=10
RECORD_PREROLL_SEC=2.0
RECORD_MAX_DURATION_SEC=60
```

I frame raw vengono prelevati dallo stream esistente; il pre-roll usa un ring buffer in
`CameraStream`. Su mini PC/NAS x86 il costo è contenuto; alza/abbassa `RECORD_FPS` per
bilanciare qualità e CPU.

## 3. Retention / pulizia automatica disco

Un thread janitor elimina periodicamente gli eventi vecchi per limitare l'uso del disco.
L'evento attualmente aperto non viene mai cancellato.

```
MOTION_RETENTION_DAYS=14        # 0 = nessun limite per età
MOTION_RETENTION_MAX_MB=5000    # 0 = nessun limite di dimensione; rimuove i più vecchi
MOTION_RETENTION_INTERVAL_SEC=3600
```

## 4. Sicurezza: password admin con hash

In produzione evita la password in chiaro: genera un hash e usalo al posto del plaintext.

```
make hash-password           # chiede la password, stampa APP_ADMIN_PASSWORD_HASH=...
```

Metti il risultato in `.env` come `APP_ADMIN_PASSWORD_HASH=...` e lascia vuoto
`APP_ADMIN_PASSWORD`. La verifica usa `werkzeug.security.check_password_hash`.

## 5. Deploy always-on (systemd + gunicorn)

Il processo deve restare **single worker** (stato in-memory + file). I thread gestiscono
la concorrenza HTTP.

```
make serve     # poetry run gunicorn -c deploy/gunicorn.conf.py app:app
```

Unit systemd di esempio in `deploy/blackframe.service` (adatta `User`,
`WorkingDirectory`, `EnvironmentFile`). Esponi in HTTPS dietro reverse proxy
(Nginx/Caddy) — vedi `docs/CONFIGURAZIONE_AVVIO_HARDENING.md`.
