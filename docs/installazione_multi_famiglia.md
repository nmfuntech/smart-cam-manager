# Installazione per più famiglie (single-tenant)

BLACKFRAME è **single-tenant**: un'installazione = un nucleo familiare = una casa.
Per servire più famiglie si installa **una copia indipendente per ciascuna**, ognuna
con i propri segreti, la propria camera e il proprio bot Telegram. Non esiste (ed è
sconsigliato creare) un server unico condiviso: l'isolamento tra famiglie è garantito
dal fatto che ogni installazione è separata.

Questo documento è il runbook da seguire **per ogni nuova famiglia**.

---

## ⚠️ Le 3 regole d'oro

1. **Mai copiare `.env` o la cartella `data/` da un'installazione all'altra.**
   Ogni installazione DEVE generare i propri segreti con `make setup`.
   - `APP_SECRET_KEY` condivisa → un cookie di sessione di una casa è valido anche
     sull'altra (furto di sessione).
   - `APP_PROFILE_ENCRYPTION_KEY` condivisa → un'installazione può **decifrare le
     password camera** di un'altra.

2. **Non rilanciare `make setup` né `make clean` su un'installazione già attiva.**
   Cancellano `.env`, la chiave di cifratura e i profili: le password camera cifrate
   diventano **irrecuperabili**. Il setup ora chiede conferma (scrivi `reset`) e
   `make clean` richiede `FORCE=1` — ma resta un'operazione distruttiva.

3. **Un bot Telegram per famiglia.** Niente bot condiviso tra case diverse. L'allowlist
   dei comandi deve contenere solo il gruppo/chat di quella famiglia.

---

## Installazione passo-passo (per ogni famiglia)

### 1. Prerequisiti
- Python 3.11+, Poetry, `ffmpeg` (per le clip riproducibili nel browser).
- La camera Tapo raggiungibile in LAN, con RTSP/ONVIF attivi e le credenziali.

### 2. Codice e dipendenze
```bash
git clone <repo> blackframe-<famiglia>
cd blackframe-<famiglia>
make install
```

### 3. Configurazione con segreti unici
```bash
make setup          # genera APP_SECRET_KEY, APP_PROFILE_ENCRYPTION_KEY, password admin
# oppure, solo i campi indispensabili:
make setup MINIMAL=1
```
Premendo Invio sui campi segreti, lo script **genera valori forti e unici** per questa
installazione e scrive `.env` con permessi `600`. Annota la password admin mostrata a
fine setup.

In produzione, sostituisci la password admin in chiaro con il suo hash:
```bash
make hash-password   # incolla l'output APP_ADMIN_PASSWORD_HASH in .env e svuota APP_ADMIN_PASSWORD
```

### 4. Telegram della famiglia
Crea un bot dedicato con [@BotFather](https://t.me/BotFather), poi:
```bash
# 1) scrivi un messaggio al bot dall'app Telegram, quindi trova il chat id:
poetry run python -m scripts.telegram_setup --token <BOT_TOKEN> --discover
# 2) prova l'invio:
poetry run python -m scripts.telegram_setup --token <BOT_TOKEN> --chat-id <CHAT_ID> --test
# 3) salva in .env e abilita le notifiche:
poetry run python -m scripts.telegram_setup --token <BOT_TOKEN> --chat-id <CHAT_ID> --write-env
```
Per il **controllo da Telegram** (snapshot, clip, PTZ, on/off) aggiungi a mano in `.env`:
```
TELEGRAM_COMMANDS_ENABLED=true
TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS=<chat id della famiglia>
```
Per far ricevere allarmi e comandi a **tutta la famiglia**, usa un **gruppo** Telegram:
imposta sia `NOTIFY_TELEGRAM_CHAT_ID` sia `TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS` sull'id
del gruppo (numero negativo). Per usare i pulsanti a etichetta nel gruppo, disabilita la
privacy mode del bot in BotFather (`/setprivacy` → Disable); i comandi `/...` e il menu
inline funzionano comunque.

> Chi è nell'allowlist ha **controllo completo** (vede snapshot/clip live, muove la PTZ,
> spegne il rilevamento). Aggiungi solo persone fidate.

### 5. Avvio
- Prova locale: `make run` → http://127.0.0.1:8000/login
- Produzione headless: avviato come **servizio** al boot, con riavvio automatico.
  Procedura per Linux (systemd), macOS (launchd) e Windows (NSSM + waitress) in
  **`docs/gestione_servizio.md`**. Una volta partito, rilevamento, registrazione,
  notifiche e comandi Telegram girano **senza bisogno di aprire la dashboard**.

### 6. Esposizione in rete (opzionale)
Se NON resti su `127.0.0.1`:
- `APP_SECRET_KEY` diventa **obbligatorio** (l'app si rifiuta di partire senza).
- Metti davanti un reverse proxy con HTTPS; imposta `APP_SESSION_COOKIE_SECURE=true`.
- Imposta `APP_TRUST_PROXY=true` **solo** se il proxy riscrive `X-Forwarded-For`
  (altrimenti il rate-limit anti brute-force del login è aggirabile).

---

## Aggiornamenti

I dati stanno fuori dal repo (`.env`, `data/`, `captures/` sono gitignored), quindi
sopravvivono agli update senza toccare i segreti:
```bash
git pull
make install
# riavvia il servizio
```
Non serve (e non va fatto) rilanciare `make setup`.

## Backup

Per ogni famiglia, salva in luogo sicuro **`.env` + `data/`** (contengono la chiave di
cifratura e i profili). Senza `data/.camera_profiles.key` i profili salvati non sono più
decifrabili. Tieni i backup di famiglie diverse **separati**.

## Revoca accesso a una persona

Togli il suo chat id da `TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS` (o falla uscire dal gruppo
usato come allowlist) e riavvia. Se sospetti una fuga del **token** del bot, revocalo da
BotFather (`/revoke`) e aggiorna `NOTIFY_TELEGRAM_BOT_TOKEN`.

## Checklist rapida per ogni installazione

- [ ] `make install`
- [ ] `make setup` → segreti **unici** generati (non copiati da altrove)
- [ ] `make hash-password` → `APP_ADMIN_PASSWORD_HASH` in `.env`, `APP_ADMIN_PASSWORD` svuotata
- [ ] Bot Telegram **dedicato** + allowlist = solo gruppo/chat di questa famiglia
- [ ] Camera configurata e stream verificato
- [ ] Avvio come servizio al boot (`make serve`)
- [ ] Se esposto in rete: HTTPS + `APP_SESSION_COOKIE_SECURE=true` (+ `APP_TRUST_PROXY` solo dietro proxy fidato)
- [ ] Backup di `.env` + `data/` archiviato separatamente
