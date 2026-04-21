# Configurazione e Avvio Dopo Hardening

Questo documento descrive come configurare e avviare l'app BLACKFRAME dopo l'hardening di sicurezza introdotto nel progetto.

## 1. Cosa cambia rispetto a prima

- L'app ascolta di default solo su `127.0.0.1`.
- L'accesso a viewer, PTZ e API amministrative richiede login.
- Le richieste mutanti sono protette con sessione e token CSRF.
- I profili camera vengono salvati con credenziali cifrate a riposo.
- Vengono applicati header di sicurezza e rate limiting di base.

## 2. Prerequisiti

- Python 3.11
- Poetry installato
- Accesso alle credenziali RTSP e ONVIF della camera

Verifica rapida:

```bash
python3 --version
poetry --version
```

## 3. Primo setup del progetto

Dal root del repository:

```bash
make setup
make lock
make install
```

Note:

- `make setup` avvia un setup interattivo che chiede tutti i parametri `.env` e genera chiavi forti per app e cifratura profili.
- `make lock` aggiorna `poetry.lock` con le dipendenze correnti, incluso `cryptography`.
- `make install` installa l'ambiente virtuale Poetry.

## 4. Preparazione del file `.env`

Copia il file di esempio:

```bash
cp .env.example .env
chmod 600 .env
```

Imposta almeno queste variabili:

```dotenv
APP_ADMIN_PASSWORD=una_password_admin_lunga_e_unica
APP_SECRET_KEY=una_stringa_lunga_random
APP_BIND_HOST=127.0.0.1

TAPO_USERNAME=utente_rtsp
TAPO_PASSWORD=password_rtsp
TAPO_HOST=192.168.1.50
TAPO_RTSP_PORT=554
TAPO_STREAM_PATH=stream1
TAPO_ONVIF_PORT=2020
TAPO_ONVIF_USERNAME=
TAPO_ONVIF_PASSWORD=
```

### Variabili importanti

- `APP_ADMIN_PASSWORD`
  Password di accesso all'interfaccia web. Non viene più derivata dalla password camera.

- `APP_SECRET_KEY`
  Segreto applicativo usato per sessione Flask e compatibilità di cifratura.

- `APP_BIND_HOST`
  Default sicuro: `127.0.0.1`.

- `APP_PROFILE_ENCRYPTION_KEY`
  Opzionale. Se impostata, viene usata per cifrare i segreti nei profili camera.
  Se non viene impostata, l'app crea una chiave locale in `data/.camera_profiles.key`.

- `APP_SESSION_COOKIE_SECURE`
  Impostarla a `true` solo quando l'app viene servita in HTTPS.

## 5. Gestione dei profili camera cifrati

### Primo avvio

Al primo avvio l'app:

- crea `data/.camera_profiles.key` se non esiste e se non è stata impostata `APP_PROFILE_ENCRYPTION_KEY`
- salva `data/camera_profiles.json` con password cifrate
- imposta permessi privati sui file (`0600`)

### Migrazione installazioni esistenti

Se esiste già un `data/camera_profiles.json` in chiaro, l'app lo migra automaticamente al primo accesso utile.

File coinvolti:

- `data/camera_profiles.json`
- `data/.camera_profiles.key`

Attenzione:

- se perdi `data/.camera_profiles.key`, non potrai più decifrare i segreti salvati
- in ambiente stabile conviene impostare esplicitamente `APP_PROFILE_ENCRYPTION_KEY`

### Rotazione credenziali consigliata

Se in passato le password camera erano state salvate in chiaro, è consigliato:

1. cambiare password RTSP / ONVIF sulla camera
2. aggiornare `.env`
3. aggiornare il profilo camera dall'interfaccia o tramite nuovo salvataggio

## 6. Avvio locale sicuro

Avvio standard:

```bash
make run
```

L'app parte su:

```text
http://127.0.0.1:8000
```

Al primo accesso:

1. apri `/login`
2. inserisci `APP_ADMIN_PASSWORD`
3. accedi a viewer e gestione camere

## 7. Avvio dietro reverse proxy HTTPS

Scenario consigliato per accesso da altri dispositivi.

### Configurazione app

Lascia l'app locale:

```dotenv
APP_BIND_HOST=127.0.0.1
APP_SESSION_COOKIE_SECURE=true
```

### Reverse proxy

Metti davanti un proxy HTTPS come Nginx, Caddy o Traefik.

Requisiti minimi:

- terminazione TLS/HTTPS
- inoltro verso `127.0.0.1:8000`
- header `X-Forwarded-For`
- accesso limitato alla rete fidata o tramite ulteriore auth di rete se necessario

## 8. Esposizione diretta su LAN

Non è l'opzione preferita.

Se necessario:

```dotenv
APP_BIND_HOST=0.0.0.0
```

Usarla solo se:

- la rete è fidata
- hai comunque HTTPS o tunnel protetto
- hai impostato `APP_ADMIN_PASSWORD` forte
- hai impostato `APP_SECRET_KEY` forte

## 9. Test rapidi dopo configurazione

### Test unitari

```bash
make test
```

### Controlli manuali

Verifica:

- accesso a `/login`
- login corretto con password admin
- viewer accessibile solo dopo login
- pagina `/cameras` protetta da login
- attivazione camera funzionante
- PTZ funzionante

### Verifica cifratura profili

Il file `data/camera_profiles.json` non deve contenere password in chiaro.

Controllo rapido:

```bash
grep -n "enc::" data/camera_profiles.json
```

## 10. Troubleshooting

### Errore: `Configura APP_ADMIN_PASSWORD prima di usare BLACKFRAME`

Manca `APP_ADMIN_PASSWORD` nel `.env`.

### Errore login con cookie sicuri in HTTP locale

Se stai lavorando in HTTP locale, non impostare:

```dotenv
APP_SESSION_COOKIE_SECURE=true
```

### I profili camera non si aprono più dopo spostamento macchina / backup

Probabile problema di chiave:

- ripristina `data/.camera_profiles.key`
- oppure configura `APP_PROFILE_ENCRYPTION_KEY` corretta

### `poetry install` non aggiorna una nuova dipendenza

Esegui prima:

```bash
make lock
make install
```

## 11. Checklist minima di produzione

- `APP_ADMIN_PASSWORD` forte e unica
- `APP_SECRET_KEY` forte e persistente
- `APP_PROFILE_ENCRYPTION_KEY` definita oppure key file protetto e backupato
- `APP_SESSION_COOKIE_SECURE=true` se dietro HTTPS
- accesso via reverse proxy HTTPS
- password camera ruotate se in passato esposte in chiaro
- `data/camera_profiles.json` e `.env` con permessi privati

## 12. Comandi utili

```bash
make setup
make lock
make install
make run
make test
```
