# Accesso all'UI dal minipc sulla rete locale

Guida per far girare BLACKFRAME sul **mini PC Windows** e raggiungere l'interfaccia web da un
altro dispositivo (es. il Mac) sulla **stessa rete locale**.

Per la prima installazione sul mini PC vedi [`installazione_windows.md`](installazione_windows.md).
Per modificare codice e configurazione da Cursor via SSH vedi
[`sviluppo_remoto_cursor_ssh.md`](sviluppo_remoto_cursor_ssh.md).

---

## 1. Aggiorna il codice sul minipc

**Windows (PowerShell nella cartella del progetto):**

```powershell
cd C:\Users\nikom\smart-cam-manager
git fetch
git pull
.\blackframe.ps1 install
```

Se hai modifiche locali non committate, mettile da parte prima con `git stash`.

**Linux** (se il minipc non è Windows): `make install` al posto di `.\blackframe.ps1 install`.

---

## 2. Configura `.env` per l'accesso in rete

Di default l'app ascolta solo su `127.0.0.1` (raggiungibile solo dal minipc
stesso). Per renderla raggiungibile dagli altri dispositivi della LAN servono due
variabili nel file `.env`:

```ini
APP_BIND_HOST=0.0.0.0
APP_SESSION_COOKIE_SECURE=false
```

Perché entrambe:

- `APP_BIND_HOST=0.0.0.0` → l'app accetta connessioni da tutta la rete locale, non
  solo da `localhost`.
- `APP_SESSION_COOKIE_SECURE=false` → fuori da `localhost` l'app marca il cookie di
  login come `Secure`, e su una connessione `http://` (senza certificato TLS) il
  browser lo scarta: il login non andrebbe a buon fine. Questa riga lo riabilita
  su `http`.

> **Nota di sicurezza.** Con `Secure=false` su `http` il cookie di sessione viaggia
> in chiaro sulla LAN. Su una rete di casa fidata è un compromesso accettabile, e
> l'accesso resta comunque protetto dalla **password admin**. Per un setup "fatto
> bene" servirebbe TLS davanti all'app (reverse proxy HTTPS), fuori dallo scopo di
> questa guida.

Verifica anche che siano presenti le variabili obbligatorie (già richieste
all'avvio): `APP_ADMIN_PASSWORD`, `APP_SECRET_KEY` (stabile — off-loopback l'app
si rifiuta di partire senza), `TAPO_USERNAME`, `TAPO_PASSWORD`.

Per usare il nuovo layer di automazione e i comandi Telegram, abilita anche:

```ini
AUTOMATION_ENABLED=true
TELEGRAM_COMMANDS_ENABLED=true
```

---

## 3. Avvia l'app (una sola istanza)

Su mini PC Windows in produzione l'app gira di solito come **servizio NSSM**
(`BLACKFRAME`). Dopo modifiche a `.env` o al codice:

```powershell
nssm restart BLACKFRAME
```

Verifica stato:

```powershell
nssm status BLACKFRAME
poetry run python scripts\windows_service.py health
```

**Test manuale in foreground** (solo se il servizio è fermo — stessa porta):

```powershell
nssm stop BLACKFRAME
.\blackframe.ps1 run
```

> **Importante:** avvia **una sola** istanza. Servizio NSSM **e** `blackframe.ps1 run`
> insieme creano due processi sulla porta 8000, duplicano i thread camera e generano
> il conflitto Telegram `terminated by other getUpdates request`.

Su **Linux/macOS** in dev: `make run` o `make serve` (gunicorn). Stesse variabili
`APP_BIND_HOST` / `APP_SESSION_COOKIE_SECURE`.

All'avvio l'output deve mostrare:

```
* Running on http://0.0.0.0:8000
```

Se invece vedi `http://127.0.0.1:8000`, `APP_BIND_HOST` non è stato letto
(controlla che `.env` sia nella cartella del progetto e che l'app lo carichi).

---

## 4. Trova l'indirizzo IP del minipc

**Windows:**

```powershell
ipconfig
```

→ riga *IPv4 Address* (es. `192.168.1.120`).

**Linux:** `hostname -I` oppure `ip addr | grep 192.168`

Suggerimento: assegna al minipc un **IP statico** o una *reservation* DHCP nel
router, così l'indirizzo non cambia nel tempo (utile anche per SSH e Cursor —
vedi [`sviluppo_remoto_cursor_ssh.md`](sviluppo_remoto_cursor_ssh.md)).

---

## 5. Apri l'UI dal Mac (o da qualsiasi dispositivo in rete)

Nel browser:

```
http://<IP-minipc>:8000
```

Esempio: `http://192.168.1.120:8000`

Poi accedi con le credenziali admin (`APP_ADMIN_PASSWORD`).

---

## 6. Firewall

Se la pagina non si apre nonostante l'app sia in esecuzione:

- **Windows:** sblocca la porta `8000` in entrata (Windows Defender Firewall →
  Regole connessioni in entrata → Nuova regola → Porta → TCP 8000 → Consenti).
- **Linux:** la porta è di solito già aperta; con `ufw` attivo:
  `sudo ufw allow 8000/tcp`.

---

## 7. Sviluppo remoto con Cursor (SSH)

Per **modificare codice, `.env`, log e servizio** sul minipc da un Mac o da un altro PC
— senza sederti davanti al mini PC — usa Cursor con **Remote SSH**.

Guida completa (OpenSSH su Windows, chiavi SSH, apertura cartella
`C:\Users\nikom\smart-cam-manager`, flusso git/Poetry/NSSM):

→ **[`docs/sviluppo_remoto_cursor_ssh.md`](sviluppo_remoto_cursor_ssh.md)**

SSH (porta **22**) e UI web (porta **8000**) sono canali separati: con Cursor lavori
sul filesystem del minipc; con il browser usi l'interfaccia BLACKFRAME.

---

## Troubleshooting rapido

| Sintomo | Causa | Rimedio |
|---|---|---|
| Pagina non si apre dal Mac | App su `127.0.0.1` o firewall | `APP_BIND_HOST=0.0.0.0` + sblocca porta 8000 |
| Login non "tiene" / torna al login | Cookie `Secure` scartato su http | `APP_SESSION_COOKIE_SECURE=false` |
| App non parte off-loopback | `APP_SECRET_KEY` mancante | Imposta un `APP_SECRET_KEY` stabile nel `.env` |
| `terminated by other getUpdates` | Due istanze attive | Tieni una sola istanza |
| `RuleConfigError: device non nel registry` | Regola punta a un device assente | Correggi/elimina la regola da `/automazione` o aggiungi il device |
