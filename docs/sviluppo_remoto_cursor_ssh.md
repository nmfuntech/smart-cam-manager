# Sviluppo remoto con Cursor via SSH (mini PC Windows)

Guida per aprire e modificare il codice BLACKFRAME **sul mini PC Windows** da un altro
computer (Mac, Linux o altro Windows) usando **Cursor** e **Remote SSH**.

> **Cosa ottieni.** Editor, terminale integrato, ricerca nel codice e Agent Cursor
> girano **sul mini PC**: vedi file, `.env`, log e processi reali dell'installazione.
>
> **Cosa non sostituisce.** L'interfaccia web BLACKFRAME resta nel browser
> (`http://<IP-minipc>:8000`). SSH serve allo **sviluppo/configurazione**, non alla
> visione live delle clip. Per l'UI in LAN vedi
> [`accesso_lan_minipc.md`](accesso_lan_minipc.md).

---

## Panoramica

```
┌─────────────────────┐         SSH :22          ┌──────────────────────────────┐
│  Mac / altro PC     │ ───────────────────────► │  Mini PC Windows             │
│  Cursor (client)    │                          │  OpenSSH Server              │
│  Remote SSH ext.    │ ◄─────────────────────── │  Cartella progetto (git)     │
└─────────────────────┘                          │  Servizio BLACKFRAME (NSSM)  │
                                                 └──────────────────────────────┘
```

| Componente | Dove gira | Porta |
|------------|-----------|-------|
| Cursor UI | Mac / PC di sviluppo | — |
| Server Remote SSH di Cursor | Mini PC Windows | dinamica (interna) |
| OpenSSH | Mini PC Windows | **22** |
| BLACKFRAME (Waitress/NSSM) | Mini PC Windows | **8000** (default) |

---

## Prerequisiti

### Sul mini PC Windows

- Windows 10/11 con BLACKFRAME già installato (wizard o manuale —
  [`installazione_windows.md`](installazione_windows.md))
- Mini PC e Mac sulla **stessa LAN** (ethernet consigliato sul mini PC)
- Utente Windows con password (es. `nikom`) e permesso di login
- Git e Poetry già funzionanti nella cartella del progetto

### Sul computer con Cursor

- [Cursor](https://cursor.com) installato
- Estensione **Remote - SSH** (in Cursor: Extensions → cerca "Remote SSH")
- Client OpenSSH (preinstallato su macOS; su Windows 10+ spesso già presente)

### Cartella progetto sul mini PC

Negli esempi del repository la root di installazione è:

```text
C:\Users\nikom\smart-cam-manager
```

Se hai clonato altrove, sostituisci con il percorso reale. Per verificarlo sul mini PC:

```powershell
cd C:\Users\nikom\smart-cam-manager
git rev-parse --show-toplevel
poetry env info --path
```

Struttura utile da conoscere una volta connessi in Cursor:

| Percorso | Contenuto |
|----------|-----------|
| `C:\Users\nikom\smart-cam-manager\` | Root repo, `.env`, `blackframe.log` |
| `C:\Users\nikom\smart-cam-manager\src\blackframe\` | Codice applicazione |
| `C:\Users\nikom\smart-cam-manager\data\` | Dati runtime (eventi, automazione) |
| `C:\Users\nikom\smart-cam-manager\captures\` | Clip registrate |
| `C:\Users\nikom\smart-cam-manager\.venv\` | Virtualenv Poetry (se presente) |

---

## Parte 1 — Abilitare SSH sul mini PC Windows

Esegui **PowerShell come amministratore** sul mini PC.

### 1.1 Installare OpenSSH Server

```powershell
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'

Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

Se la capability risulta già `State : Installed`, passa al passo successivo.

### 1.2 Avviare il servizio e impostare avvio automatico

```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Get-Service sshd
```

Lo stato deve essere `Running`.

### 1.3 Firewall (porta 22)

Di solito Windows crea la regola `OpenSSH-Server-In-TCP`. Verifica:

```powershell
Get-NetFirewallRule -Name *OpenSSH* | Format-Table Name, Enabled, Direction
```

Se manca, creala (rete **Privata**):

```powershell
New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Private
```

> **Sicurezza.** Esporre SSH solo sulla LAN di casa. Non inoltrare la porta 22 sul
> router verso Internet senza hardening aggiuntivo (fail2ban, chiavi only, VPN).

### 1.4 Trovare l'IP del mini PC

```powershell
ipconfig
```

Annota l'**IPv4** della scheda connessa alla LAN, es. `192.168.1.120`.

Consiglio: **IP statico** o DHCP reservation sul router, così la configurazione SSH
non si rompe dopo un reboot.

### 1.5 Test locale sul mini PC

```powershell
ssh localhost
```

Accetta l'host key al primo prompt, inserisci la password Windows. Se entri in una
shell, OpenSSH funziona.

---

## Parte 2 — Chiave SSH dal Mac (o PC di sviluppo)

L'autenticazione a chiave evita di digitare la password a ogni connessione Cursor.

### 2.1 Generare la chiave (sul Mac)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_blackframe_minipc -C "cursor-blackframe-minipc"
```

Lascia passphrase vuota solo se il Mac è personale e protetto; altrimenti impostala.

### 2.2 Copiare la chiave pubblica sul mini PC

Sostituisci `nikom` e `192.168.1.120` con i tuoi valori.

**Da macOS / Linux:**

```bash
cat ~/.ssh/id_ed25519_blackframe_minipc.pub | ssh nikom@192.168.1.120 \
  "powershell -NoProfile -Command \"\$d=Join-Path \$env:USERPROFILE '.ssh'; New-Item -ItemType Directory -Force -Path \$d | Out-Null; \$f=Join-Path \$d 'authorized_keys'; \$input | Out-File -FilePath \$f -Encoding utf8 -Append\""
```

In alternativa, sul mini PC crea manualmente
`C:\Users\nikom\.ssh\authorized_keys` e incolla **una riga** del file `.pub`.

### 2.3 Permessi su Windows (importante)

OpenSSH su Windows rifiuta chiavi se i permessi ACL sono troppo permissivi. Sul mini PC,
PowerShell **come utente `nikom`** (non necessariamente admin):

```powershell
$sshDir  = "$env:USERPROFILE\.ssh"
$authKeys = "$sshDir\authorized_keys"

icacls $sshDir /inheritance:r
icacls $sshDir /grant "${env:USERNAME}:(OI)(CI)F"

icacls $authKeys /inheritance:r
icacls $authKeys /grant "${env:USERNAME}:(R)"
```

### 2.4 Verificare `sshd_config` (solo se la chiave non funziona)

File: `C:\ProgramData\ssh\sshd_config`. Devono essere attivi:

```text
PubkeyAuthentication yes
PasswordAuthentication yes
```

Dopo modifiche, **PowerShell admin**:

```powershell
Restart-Service sshd
```

### 2.5 Test dal Mac

```bash
ssh -i ~/.ssh/id_ed25519_blackframe_minipc nikom@192.168.1.120
```

Deve entrare **senza** chiedere la password Windows. Se funziona, Cursor potrà
connettersi.

---

## Parte 3 — Configurare `~/.ssh/config` sul Mac

Crea o modifica `~/.ssh/config`:

```ssh-config
Host blackframe-minipc
    HostName 192.168.1.120
    User nikom
    IdentityFile ~/.ssh/id_ed25519_blackframe_minipc
    IdentitiesOnly yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

Test rapido:

```bash
ssh blackframe-minipc
```

Opzionale — se la connessione è lenta o instabile su Wi‑Fi, aggiungi:

```ssh-config
    Compression yes
```

---

## Parte 4 — Connettere Cursor via Remote SSH

### 4.1 Prima connessione

1. Apri **Cursor** sul Mac.
2. `Cmd+Shift+P` (Windows/Linux: `Ctrl+Shift+P`) → digita **Remote-SSH: Connect to Host...**
3. Scegli **`blackframe-minipc`** (o inserisci `nikom@192.168.1.120`).
4. Cursor chiede la **piattaforma remota**: seleziona **Windows**.
5. Attendi il download/installazione del **Cursor Server** sul mini PC (solo la prima
   volta; richiede rete e qualche minuto).
6. In basso a sinistra compare **`SSH: blackframe-minipc`** → connessione attiva.

### 4.2 Aprire la cartella progetto

1. **File → Open Folder...** (o **Open Workspace from SSH...**).
2. Percorso:

   ```text
   C:\Users\nikom\smart-cam-manager
   ```

3. Conferma **Trust** / fiducia workspace se richiesto.

Cursor indicizza il repo sul mini PC. Il **terminale integrato** (`Ctrl+`` `) è
PowerShell **sul mini PC**, già nella cartella se apri il terminale dopo aver aperto
la folder.

### 4.3 Verificare l'ambiente nel terminale Cursor

```powershell
pwd
git status
poetry --version
poetry run python -c "import blackframe; print('ok')"
Get-Content .env | Select-String "APP_BIND_HOST|APP_PORT"
```

### 4.4 Impostazioni utili in Cursor

| Impostazione | Valore consigliato | Motivo |
|--------------|-------------------|--------|
| Remote.SSH: Remote Server Listen On Socket | default | di solito ok su LAN |
| Terminal › Integrated › Default Profile: Windows | PowerShell | coerente con script `.ps1` |
| Files: Exclude | `**/captures/**`, `**/.venv/**` | meno rumore in ricerca |

Per Agent / indexing: la cartella `captures/` può essere grande; escluderla migliora
le performance se non lavori sulle clip.

---

## Parte 5 — Flusso di lavoro quotidiano

### Modificare codice e testare

1. Connetti Cursor via SSH e apri `C:\Users\nikom\smart-cam-manager`.
2. Modifica file sotto `src\blackframe\`.
3. Nel terminale integrato:

   ```powershell
   # Se il servizio NSSM è attivo, fermalo prima di test manuali sulla stessa porta
   nssm status BLACKFRAME
   nssm stop BLACKFRAME

   poetry run python deploy\serve_waitress.py
   ```

4. Dal Mac, apri l'UI: `http://192.168.1.120:8000` (se `APP_BIND_HOST=0.0.0.0`).

> **Una sola istanza.** Non lasciare `serve_waitress.py` **e** il servizio NSSM attivi
> insieme: stessa porta, doppio processo, conflitti Telegram/camera. Vedi
> [`installazione_windows.md`](installazione_windows.md#due-processi-sulla-porta-8000).

### Aggiornare il codice da git

```powershell
nssm stop BLACKFRAME
git fetch
git pull
poetry install --with windows
poetry run python scripts\check_prerequisites.py
nssm start BLACKFRAME
```

`.env`, `data/` e `captures/` non vengono toccati da `git pull`.

### Leggere log e stato servizio

```powershell
Get-Content C:\Users\nikom\smart-cam-manager\blackframe.log -Tail 80 -Wait
poetry run python scripts\windows_service.py status
poetry run python scripts\windows_service.py health
```

### Modificare `.env` in sicurezza

Il file `.env` è nella root del progetto. Dopo modifiche:

```powershell
nssm restart BLACKFRAME
```

L'app legge `.env` all'avvio dalla working directory del servizio (root progetto).

### Comandi rapidi senza `make` (Windows)

```powershell
.\blackframe.ps1 help
.\blackframe.ps1 check-prerequisites
.\blackframe.ps1 serve
```

---

## Parte 6 — Cursor su un secondo PC Windows (client)

Se usi Cursor anche da un PC Windows verso il mini PC:

1. Stessi passi SSH: chiave in `%USERPROFILE%\.ssh\` sul client, voce in
   `%USERPROFILE%\.ssh\config` analoga al Mac.
2. In Cursor: **Remote-SSH: Connect to Host...** → `blackframe-minipc`.
3. **Open Folder** → `C:\Users\nikom\smart-cam-manager`.

Il client può essere qualsiasi macchina sulla LAN; il codice resta solo sul mini PC.

---

## Troubleshooting

| Sintomo | Causa probabile | Rimedio |
|---------|-----------------|---------|
| `Connection refused` porta 22 | `sshd` fermo o firewall | `Start-Service sshd`; regola firewall TCP 22 |
| `Connection timed out` | IP sbagliato, mini PC spento, altra subnet | `ping 192.168.x.x`; verifica LAN e IP statico |
| Chiede sempre password Windows | Chiave non in `authorized_keys` o ACL errati | Rifare Parte 2.2–2.3 |
| `Permission denied (publickey)` | `PubkeyAuthentication` off o file `.ssh` di altro utente | Verifica utente SSH = proprietario di `.ssh` |
| Cursor si blocca su "Installing server" | Antivirus/firewall blocca download | Attendi; riprova; controlla spazio disco su `C:` |
| Cartella vuota o path errato | Repo in percorso diverso | `git rev-parse --show-toplevel` sul mini PC |
| Terminale non trova `poetry` | PATH utente vs servizio | Apri nuovo terminale; verifica `poetry` in sessione interattiva |
| Modifiche non visibili in UI | Servizio non riavviato | `nssm restart BLACKFRAME` |
| Porta 8000 occupata | Doppia istanza | `netstat -ano \| findstr ":8000.*LISTENING"`; ferma una delle due |
| Agent Cursor lento | Repo grande + `captures/` indicizzate | Escludi `captures/` e `.venv` da Files |

### Log OpenSSH su Windows

```powershell
Get-WinEvent -LogName "OpenSSH/Operational" -MaxEvents 20 | Format-List
```

Utile se la chiave viene rifiutata.

### Disconnettere Cursor dal remoto

`Cmd+Shift+P` → **Remote-SSH: Close Remote Connection**.

Il servizio BLACKFRAME e `sshd` restano attivi sul mini PC.

---

## Sicurezza — riepilogo

- Usa **chiavi SSH** e password Windows forte sull'utente del mini PC.
- Tieni SSH **solo in LAN**; non esporre la porta 22 su Internet.
- Il `.env` sul mini PC contiene segreti (Tapo, Telegram, `APP_SECRET_KEY`): trattalo
  come produzione; non committarlo.
- SSH dà accesso **completo** al mini PC con i permessi dell'utente Windows scelto.

---

## Riferimenti

| Documento | Contenuto |
|-----------|-----------|
| [`installazione_windows.md`](installazione_windows.md) | Wizard, NSSM, aggiornamento repo |
| [`accesso_lan_minipc.md`](accesso_lan_minipc.md) | UI web da browser in LAN |
| [`gestione_servizio.md`](gestione_servizio.md) | Avvio, restart, boot |
| [`report-mini-pc-windows.md`](report-mini-pc-windows.md) | Tuning CPU/stream mini PC |
