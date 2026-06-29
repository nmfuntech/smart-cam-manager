# Installer Windows (Inno Setup)

Crea un pacchetto `BLACKFRAME-Setup-x.y.z.exe` distribuibile, integrato nel flusso di sviluppo.

## Prerequisiti (solo sulla macchina di build)

1. **Inno Setup 6** — https://jrsoftware.org/isdl.php  
   (viene rilevato automaticamente in `C:\Program Files (x86)\Inno Setup 6\`)

2. **Poetry** + dipendenze già installate nel repo (`poetry install --with windows`)

3. Connessione internet (prima build: scarica Python embed e pip)

## Creare l'installer

```powershell
.\blackframe.ps1 build-installer
```

Con modello classificazione incluso (~180 MB in più):

```powershell
.\blackframe.ps1 build-installer -WithModel
```

Solo staging (senza compilare `.exe`, utile per debug):

```powershell
.\blackframe.ps1 build-installer -SkipCompile
```

Output:

| Artefatto | Percorso |
|-----------|----------|
| Installer | `dist\BLACKFRAME-Setup-0.1.0.exe` |
| Staging | `dist\blackframe-staging\` |
| Cache build | `build\cache\` (Python embed, get-pip) |

La versione è letta da `pyproject.toml` (`version = "..."`).

## Cosa contiene il pacchetto

- Codice applicazione (senza `.git`, `.venv`, `captures`, `.env`)
- **Runtime Python portabile** in `runtime\python\` + dipendenze in `Lib\site-packages\`
- Template `.env.windows-minipc.example`
- Script post-install: dati in `C:\ProgramData\BLACKFRAME\`
- Opzionale: registrazione servizio NSSM + wizard configurazione

## Flusso installazione per l'utente finale

1. Esegue `BLACKFRAME-Setup-x.y.z.exe` (richiede admin)
2. File programma → `C:\Program Files\BLACKFRAME\`
3. Dati runtime → `C:\ProgramData\BLACKFRAME\` (`.env`, `captures\`, `data\`, log)
4. Task opzionale: wizard credenziali + servizio NSSM
5. Browser → http://127.0.0.1:8000

La disinstallazione **non** cancella `C:\ProgramData\BLACKFRAME\` (clip e configurazione restano).

## Dopo modifiche al codice

```powershell
git pull
poetry install --with windows
.\blackframe.ps1 build-installer
```

Distribuisci il nuovo `dist\BLACKFRAME-Setup-*.exe`.

## Firma digitale (produzione)

Per evitare l'avviso SmartScreen, firma l'installer con un certificato Authenticode:

```powershell
signtool sign /fd SHA256 /a "dist\BLACKFRAME-Setup-0.1.0.exe"
```

## File coinvolti

| File | Ruolo |
|------|--------|
| `deploy/blackframe.iss` | Script Inno Setup |
| `scripts/build_windows_installer.ps1` | Staging + compilazione |
| `deploy/post_install.ps1` | Setup ProgramData + wizard + NSSM |
| `deploy/uninstall.ps1` | Rimuove servizio NSSM |
| `scripts/runtime_paths.py` | `BLACKFRAME_HOME` / ProgramData |
