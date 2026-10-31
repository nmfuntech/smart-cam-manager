# Sviluppo sul Mac, deploy sul mini PC Windows

Il codice si scrive e si testa sul Mac. Sul mini PC arriva solo tramite git,
con un aggiornamento che verifica tutto prima di toccare il servizio e torna
indietro da solo se qualcosa va storto.

## Il flusso in breve

```bash
# sul Mac, con le modifiche già committate
make deploy                 # deploy del branch corrente
make deploy REF=main        # deploy di un branch, tag o commit preciso
make deploy-status          # cosa sta girando sul mini PC
```

`make deploy` esegue nell'ordine:

1. controlla che non ci siano modifiche non committate;
2. lancia lint e test sul Mac;
3. fa `git push` del branch;
4. copia sul mini PC l'ultima versione di `scripts/windows/update.ps1` e la
   esegue via SSH.

Sul mini PC, `update.ps1`:

1. si rifiuta di procedere se qualcuno ha modificato file tracciati lì sopra;
2. fa `git fetch` e checkout del riferimento richiesto;
3. reinstalla le dipendenze **solo** se `pyproject.toml` o `poetry.lock` sono
   cambiati (`poetry sync --with windows`);
4. scarica il modello YOLOX se il backend lo richiede e manca;
5. lancia l'intera suite di test in un **git worktree separato**, mai nella
   cartella live (alcuni test simulano `.env` e dati);
6. configura i log del servizio in `logs\` e sposta i vecchi log;
7. riavvia il servizio e attende che `/health` risponda con il **nuovo**
   commit;
8. se il servizio non torna sano entro 120 secondi, rimette il commit
   precedente e riavvia.

Codici di uscita: `0` ok, `2` modifiche locali sul mini PC (nulla cambiato),
`3` test o dipendenze falliti prima del riavvio (nulla cambiato), `4` versione
nuova non sana con rollback automatico.

Lo storico è in `logs\deploy.log` sul mini PC.

## Cose che servono su Windows e non sul Mac

- **Servizio NSSM** (`BLACKFRAME`, account LocalSystem): il servizio non vede
  il PATH dell'utente. Per questo ffmpeg viene cercato anche nelle cartelle di
  winget, Chocolatey e `C:\ffmpeg` (o si imposta `FFMPEG_BIN`). Senza ffmpeg le
  clip restano in mp4v e non si riproducono nel browser.
- **Log:** NSSM scrive stdout/stderr in `logs\blackframe.log` (solo warning e
  crash) e ruota il file a ogni avvio. Il log completo, con rotazione per
  dimensione, è `logs\app.log` (`LOG_FILE`, aggiunto al `.env` da
  `update.ps1`). **Non attivare mai `AppRotateOnline`**: in NSSM 2.24 blocca
  il servizio (vedi sotto).
- **Permessi file:** `chmod` su Windows non protegge nulla. All'avvio l'app
  toglie a Users, Authenticated Users ed Everyone l'accesso a `data\`,
  `captures\`, `logs\` e `.env`, tramite ACL. Owner, Administrators e SYSTEM
  restano invariati.
- **PowerShell 5.1:** gli script `.ps1` sono solo ASCII (la 5.1 legge male
  l'UTF-8 senza BOM).
- **Test:** 6 test sui permessi POSIX sono saltati su Windows; la CI li esegue
  su Linux.

## CI

GitHub Actions (`.github/workflows/ci.yml`) esegue lint e test su
`ubuntu-latest` e `windows-latest` a ogni push su `main` e `feat/**`.

## Se il servizio resta bloccato in "STOP_PENDING"

Succede se NSSM smette di rispondere (è capitato con `AppRotateOnline=1`).
Da un prompt **amministratore** sul mini PC:

```powershell
sc queryex BLACKFRAME            # annota il PID
taskkill /F /PID <PID>
C:\Tools\nssm\nssm.exe set BLACKFRAME AppRotateOnline 0
C:\Tools\nssm\nssm.exe start BLACKFRAME
```

In alternativa basta riavviare il mini PC. Dopo, dal Mac, `make deploy`
riporta il servizio sulla versione voluta con tutti i controlli.

## Aggiornare a mano sul mini PC

```powershell
cd C:\Users\nikom\smart-cam-manager
.\blackframe.ps1 update main
```
