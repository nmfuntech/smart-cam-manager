# Gestione di BLACKFRAME come servizio (avvio, restart, boot)

BLACKFRAME deve girare **sempre acceso** e **ripartire da solo** dopo un crash o
un riavvio del dispositivo. Senza Docker ci si appoggia al gestore di servizi del
sistema operativo. Tutti i file di esempio sono in `deploy/`.

> L'app legge `.env` dalla **working directory** all'avvio (`load_dotenv()`),
> quindi a ogni servizio basta puntare alla cartella di installazione: nessuna
> iniezione di variabili d'ambiente per-piattaforma.

Negli esempi l'installazione è in `/opt/blackframe` (Linux/macOS) con utente
dedicato `blackframe`. Adatta percorsi e utente al tuo caso.

---

## Linux — systemd (consigliato)

File: `deploy/blackframe.service`. Usa `gunicorn` (single worker obbligatorio) e
include hardening + `Restart=always`.

**Installazione (una tantum):**
```bash
sudo useradd -r -s /usr/sbin/nologin blackframe
sudo cp -r . /opt/blackframe && cd /opt/blackframe
make install
# ... make setup, .env, ecc.
sudo chown -R blackframe:blackframe /opt/blackframe
sudo cp deploy/blackframe.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blackframe   # avvia ORA + a ogni boot
```

**Gestione:**
| Azione | Comando |
|---|---|
| Avvia | `sudo systemctl start blackframe` |
| Ferma | `sudo systemctl stop blackframe` |
| Riavvia | `sudo systemctl restart blackframe` |
| Stato | `systemctl status blackframe` |
| Log dal vivo | `journalctl -u blackframe -f` |
| Disattiva al boot | `sudo systemctl disable blackframe` |

Auto-restart su crash/uscita (`Restart=always`, `RestartSec=5`) e ripartenza al
boot (`WantedBy=multi-user.target`) sono già configurati.

---

## macOS — launchd

File: `deploy/com.blackframe.app.plist`. Anche su macOS gira `gunicorn`, quindi
riusa `deploy/gunicorn.conf.py`.

**Installazione come daemon di sistema (parte al boot senza login):**
```bash
sudo cp deploy/com.blackframe.app.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.blackframe.app.plist
sudo launchctl load /Library/LaunchDaemons/com.blackframe.app.plist
```

**Gestione:**
| Azione | Comando |
|---|---|
| Avvia / abilita al boot | `sudo launchctl load /Library/LaunchDaemons/com.blackframe.app.plist` |
| Ferma / disabilita | `sudo launchctl unload /Library/LaunchDaemons/com.blackframe.app.plist` |
| Riavvia | `unload` seguito da `load` |
| Stato | `sudo launchctl list \| grep blackframe` |
| Log | `tail -f /opt/blackframe/blackframe.log` |

`RunAtLoad` + `KeepAlive` garantiscono avvio al boot e riavvio automatico su
crash/uscita.

---

## Windows — NSSM + waitress

Su Windows `gunicorn` **non** è disponibile: si usa `waitress` tramite
`deploy/serve_waitress.py`, registrato come **servizio** con
[NSSM](https://nssm.cc/) (così parte al boot e si riavvia da solo).

**Preparazione (una tantum):**
```powershell
# nella cartella dell'app, con il venv Poetry attivo
poetry run pip install waitress
# prova manuale:
poetry run python deploy/serve_waitress.py
```

**Registra il servizio con NSSM** (scarica `nssm.exe` e mettilo nel PATH):
```powershell
nssm install BLACKFRAME "C:\blackframe\.venv\Scripts\python.exe" "deploy\serve_waitress.py"
nssm set BLACKFRAME AppDirectory "C:\blackframe"
nssm set BLACKFRAME Start SERVICE_AUTO_START
nssm set BLACKFRAME AppStdout "C:\blackframe\blackframe.log"
nssm set BLACKFRAME AppStderr "C:\blackframe\blackframe.log"
nssm start BLACKFRAME
```
NSSM riavvia automaticamente il processo se termina, e l'avvio automatico al boot
è dato da `SERVICE_AUTO_START`.

**Gestione:**
| Azione | Comando |
|---|---|
| Avvia | `nssm start BLACKFRAME` (o `sc start BLACKFRAME`) |
| Ferma | `nssm stop BLACKFRAME` |
| Riavvia | `nssm restart BLACKFRAME` |
| Stato | `sc query BLACKFRAME` |
| Rimuovi servizio | `nssm remove BLACKFRAME confirm` |
| Log | `C:\blackframe\blackframe.log` |

> Alternativa rapida senza NSSM: il **Task Scheduler** con trigger "All'avvio del
> computer" che lancia `poetry run python deploy/serve_waitress.py` (vedi
> `docs/installazione_windows.md`). Più semplice, ma senza restart automatico su
> crash: per un impianto sempre attivo NSSM è preferibile.

---

## Dopo l'installazione

Verifica che risponda: `http://<ip-dispositivo>:<porta>/health` → `{"status":"ok"}`.
Per l'aggiornamento: ferma il servizio → `git pull && make install` → riavvia il
servizio. I dati (`.env`, `data/`, `captures/`) non vengono toccati.
