# Installa BLACKFRAME su Windows (mini PC / desktop).
# Esegui dalla root del progetto in PowerShell:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\scripts\install_windows.ps1
#
# Opzioni:
#   -SetupEnv         setup interattivo .env (solo se mancante, salvo -ForceSetup)
#   -FullEnv          setup completo (non minimale)
#   -ForceSetup       rilancia setup anche se .env esiste (richiede conferma reset)
#   -TuneMiniPc       applica profilo tuning mini PC al .env esistente
#   -SkipTools        non installa Python/Git/Poetry/ffmpeg via winget
#   -SkipDeps         non esegue poetry install
#   -SkipModel        non scarica il modello detection
#   -SkipFfmpeg       non installa ffmpeg
#   -Run              avvia l'app al termine
#   -OpenFirewall     apre porta APP_PORT nel firewall (richiede admin)

[CmdletBinding()]
param(
    [switch]$SetupEnv,
    [switch]$FullEnv,
    [switch]$ForceSetup,
    [switch]$TuneMiniPc,
    [switch]$SkipTools,
    [switch]$SkipDeps,
    [switch]$SkipModel,
    [switch]$SkipFfmpeg,
    [switch]$Run,
    [switch]$OpenFirewall,
    [int]$AppPort = $(if ($env:APP_PORT) { [int]$env:APP_PORT } else { 8000 })
)

$ErrorActionPreference = "Stop"
$AppName = "BLACKFRAME"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

function Write-Log([string]$Message) {
    Write-Host ""
    Write-Host "[$AppName] $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Host ""
    Write-Host "[$AppName] WARNING: $Message" -ForegroundColor Yellow
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Ensure-WingetPackage {
    param(
        [string]$Id,
        [string]$Label
    )
    if (-not (Test-Command winget)) {
        Write-Warn "winget non disponibile: installa manualmente $Label"
        return
    }
    Write-Log "Installo/verifico $Label (winget: $Id)"
    winget install --id $Id --exact --source winget `
        --accept-package-agreements --accept-source-agreements 2>$null | Out-Null
}

function Refresh-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($machine -or $user) {
        $env:Path = ($machine, $user -ne $null | Where-Object { $_ }) -join ";"
    }
}

function Find-Python {
    $candidates = @(
        @("py", @("-3.13")),
        @("py", @("-3.12")),
        @("py", @("-3.11")),
        @("python", @()),
        @("python3", @())
    )
    foreach ($item in $candidates) {
        $cmd = $item[0]
        $args = $item[1]
        if (-not (Test-Command $cmd)) { continue }
        & $cmd @args -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Command = $cmd; Args = $args }
        }
    }
    return $null
}

function Invoke-Python {
    param([string[]]$ScriptArgs)
    if ($script:Py.Command -eq "py") {
        & py @($script:Py.Args + $ScriptArgs)
    } else {
        & $script:Py.Command @ScriptArgs
    }
}

function Find-Poetry {
    if (Test-Command poetry) { return "poetry" }
    $poetryExe = Join-Path $env:APPDATA "Python\Scripts\poetry.exe"
    if (Test-Path $poetryExe) { return $poetryExe }
    return $null
}

function Install-Poetry {
    Write-Log "Installo Poetry"
    $tmp = [System.IO.Path]::GetTempFileName() + ".py"
    try {
        Invoke-WebRequest -Uri "https://install.python-poetry.org" -OutFile $tmp -UseBasicParsing
        Invoke-Python @($tmp)
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
    $scriptsDir = Join-Path $env:APPDATA "Python\Scripts"
    if (Test-Path $scriptsDir) {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$scriptsDir*") {
            [Environment]::SetEnvironmentVariable(
                "Path",
                ($userPath.TrimEnd(";") + ";" + $scriptsDir).TrimStart(";"),
                "User"
            )
        }
    }
    Refresh-PathFromRegistry
}

function Install-ProjectDeps {
    Write-Log "Installo dipendenze Python (poetry install --with windows)"
    & $script:Poetry install --with windows
    Write-Log "Verifico import principali"
    & $script:Poetry run python -c "import cv2, flask, dotenv, cryptography, waitress; print('OK')"
}

function Install-DetectionModel {
    Write-Log "Scarico modello classificazione (MobileNet-SSD)"
    & $script:Poetry run python scripts/fetch_model.py
}

function Run-EnvSetup {
    if ($SetupEnv -or $ForceSetup -or -not (Test-Path ".env")) {
        if ((Test-Path ".env") -and -not $ForceSetup -and -not $SetupEnv) {
            Write-Log ".env presente (usa -SetupEnv per riconfigurare)"
            return
        }
        Write-Log "Setup .env interattivo"
        $setupArgs = @("scripts/setup_config.py")
        if (-not $FullEnv) { $setupArgs += "--minimal" }
        if ($ForceSetup) { $setupArgs += "--force" }
        & $script:Poetry run python @setupArgs
    } else {
        Write-Log ".env presente"
    }
}

function Apply-MiniPcTune {
    if (-not (Test-Path ".env")) {
        Write-Warn ".env mancante: salto tuning mini PC"
        return
    }
    Write-Log "Applico profilo tuning mini-pc-windows al .env"
    & $script:Poetry run python scripts/env_profiles.py --profile mini-pc-windows
}

function Write-Launcher {
    $bat = @"
@echo off
cd /d "$ProjectDir"
poetry run python scripts\check_prerequisites.py
if errorlevel 1 (
  echo.
  echo [BLACKFRAME] Prerequisiti mancanti. Vedi messaggi sopra.
  echo Installa ffmpeg: winget install Gyan.FFmpeg
  echo Poi riapri il terminale e rilancia.
  pause
  exit /b 1
)
poetry run python deploy\serve_waitress.py >> "$ProjectDir\blackframe.log" 2>&1
"@
    Set-Content -Path (Join-Path $ProjectDir "start_blackframe.bat") -Value $bat -Encoding ASCII
    Write-Log "Creato start_blackframe.bat"
}

function Open-FirewallPort {
    $ruleName = "BLACKFRAME"
    Write-Log "Configuro firewall per TCP $AppPort"
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
            -LocalPort $AppPort -Action Allow -Profile Private | Out-Null
    }
    Write-Host "Firewall OK"
}

# --- main ---
Write-Log "Installazione in $ProjectDir"

if (-not $SkipTools) {
    if (Test-Command winget) {
        if (-not (Find-Python)) {
            Ensure-WingetPackage "Python.Python.3.11" "Python 3.11"
            Refresh-PathFromRegistry
        }
        if (-not (Test-Command git)) {
            Ensure-WingetPackage "Git.Git" "Git"
            Refresh-PathFromRegistry
        }
        if (-not $SkipFfmpeg) {
            Ensure-WingetPackage "Gyan.FFmpeg" "FFmpeg"
            Refresh-PathFromRegistry
        }
        Ensure-WingetPackage "Microsoft.VCRedist.2015+.x64" "Visual C++ Redistributable"
    } else {
        Write-Warn "winget non trovato: verifica manualmente Python 3.11+, Git e ffmpeg"
    }
}

$script:Py = Find-Python
if (-not $script:Py) {
    throw "Python 3.11+ non trovato. Installa Python e riapri PowerShell."
}
Write-Log ("Python OK: " + (Invoke-Python @("-c", "import sys; print(sys.version.split()[0])")))

if (-not (Test-Command git)) {
    throw "Git non trovato."
}
Write-Log ("Git OK: " + (git --version))

$script:Poetry = Find-Poetry
if (-not $script:Poetry -and -not $SkipTools) {
    Install-Poetry
    $script:Poetry = Find-Poetry
}
if (-not $script:Poetry) {
    throw "Poetry non trovato."
}
Write-Log ("Poetry OK: " + (& $script:Poetry --version))

if (-not $SkipDeps) {
    Install-ProjectDeps
}

if (-not $SkipModel) {
    Install-DetectionModel
}

Run-EnvSetup

if ($TuneMiniPc) {
    Apply-MiniPcTune
} elseif (Test-Path ".env") {
    # Su installazione Windows applica il profilo se il .env non ha ancora tuning MOG2.
    $envText = Get-Content ".env" -Raw
    if ($envText -notmatch "MOTION_SCALE_WIDTH=") {
        Apply-MiniPcTune
    }
}

Write-Launcher

Write-Log "Verifica prerequisiti"
& $script:Poetry run python scripts/check_prerequisites.py

if ($OpenFirewall) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if ($isAdmin) {
        Open-FirewallPort
    } else {
        Write-Warn "Firewall: riesegui come amministratore con -OpenFirewall"
    }
}

Write-Host ""
Write-Host "[$AppName] Installazione completata." -ForegroundColor Green
Write-Host ""
Write-Host "Comandi utili:"
Write-Host "  poetry run python deploy\serve_waitress.py"
Write-Host "  .\start_blackframe.bat"
Write-Host "  poetry run python scripts\check_prerequisites.py"
Write-Host "  poetry run python scripts\env_profiles.py --profile mini-pc-windows"
Write-Host ""
Write-Host "Browser: http://127.0.0.1:$AppPort"
Write-Host ""
Write-Host "Nota: dopo l'installazione di ffmpeg chiudi e riapri PowerShell," 
Write-Host "      poi verifica con: ffmpeg -version"

if ($Run) {
    if (-not (Test-Path ".env")) {
        throw ".env mancante: riesegui con -SetupEnv"
    }
    Write-Log "Avvio app (Ctrl+C per fermare)"
    & $script:Poetry run python deploy/serve_waitress.py
}
