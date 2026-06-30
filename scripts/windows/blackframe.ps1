# Comandi rapidi BLACKFRAME su Windows (alternativa a `make` su Linux/macOS).
# Uso dalla root del progetto:
#   .\blackframe.ps1 help
#   .\blackframe.ps1 install-windows
#   .\blackframe.ps1 run

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

function Find-Poetry {
    if (Get-Command poetry -ErrorAction SilentlyContinue) { return "poetry" }
    $poetryExe = Join-Path $env:APPDATA "Python\Scripts\poetry.exe"
    if (Test-Path $poetryExe) { return $poetryExe }
    return $null
}

function Invoke-PoetryPython {
    param([string[]]$Args)
    $poetry = Find-Poetry
    if (-not $poetry) {
        throw "Poetry non trovato. Esegui prima: .\blackframe.ps1 install-windows"
    }
    & $poetry run python @Args
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Show-Help {
    Write-Host @"
BLACKFRAME — comandi Windows (senza make)

  .\blackframe.ps1 install-windows     Wizard installazione completa mini PC
  .\blackframe.ps1 install             poetry install --with windows
  .\blackframe.ps1 setup               Setup interattivo .env
  .\blackframe.ps1 run                 Avvio dev (Flask, foreground)
  .\blackframe.ps1 serve               Avvio produzione (waitress)
  .\blackframe.ps1 fetch-model         Scarica modello classificazione
  .\blackframe.ps1 check-prerequisites Verifica ffmpeg, modelli, tuning
  .\blackframe.ps1 env-example        Rigenera .env.windows-minipc.example
  .\blackframe.ps1 build-installer    Crea BLACKFRAME-Setup-x.y.z.exe (Inno Setup)
  .\blackframe.ps1 test                Esegue pytest
  .\blackframe.ps1 hash-password       Genera APP_ADMIN_PASSWORD_HASH

Equivalenti diretti (se preferisci):
  poetry install --with windows
  poetry run python scripts\setup_config.py --minimal
  poetry run python deploy\serve_waitress.py
  poetry run python scripts\fetch_model.py

Documentazione:
  docs\installazione_windows.md       Installazione e wizard mini PC
  docs\accesso_lan_minipc.md          UI web da browser in LAN
  docs\sviluppo_remoto_cursor_ssh.md  Cursor Remote SSH (dev da Mac/altro PC)
  docs\gestione_servizio.md           NSSM, restart, boot
"@
}

switch ($Command.ToLower()) {
    "help" { Show-Help }
    "install-windows" {
        & "$Root\scripts\install_windows.ps1" @Rest
    }
    "install" {
        $poetry = Find-Poetry
        if (-not $poetry) { throw "Poetry non trovato." }
        & $poetry install --with windows
    }
    "setup" {
        $args = @("scripts/setup_config.py")
        if ($Rest -contains "--full") { } else { $args += "--minimal" }
        Invoke-PoetryPython $args
    }
    "run" {
        Invoke-PoetryPython @("-m", "blackframe")
    }
    "serve" {
        Invoke-PoetryPython @("deploy/serve_waitress.py")
    }
    "fetch-model" {
        Invoke-PoetryPython @("scripts/fetch_model.py")
    }
    "check-prerequisites" {
        Invoke-PoetryPython @("scripts/check_prerequisites.py")
    }
    "env-example" {
        Invoke-PoetryPython @("scripts/env_profiles.py", "--write-windows-example")
    }
    "build-installer" {
        & "$Root\scripts\build_windows_installer.ps1" @Rest
    }
    "test" {
        $poetry = Find-Poetry
        if (-not $poetry) { throw "Poetry non trovato." }
        & $poetry run python -m pytest -v @Rest
    }
    "hash-password" {
        $poetry = Find-Poetry
        if (-not $poetry) { throw "Poetry non trovato." }
        & $poetry run python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; pw=getpass('Password admin: '); print(''); print('Aggiungi al .env:'); print('APP_ADMIN_PASSWORD_HASH='+generate_password_hash(pw))"
    }
    default {
        Write-Host "Comando sconosciuto: $Command" -ForegroundColor Red
        Show-Help
        exit 1
    }
}
