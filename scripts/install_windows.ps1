# Wizard di installazione BLACKFRAME su Windows mini PC.
# Esegui dalla root del progetto in PowerShell:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\scripts\install_windows.ps1
#
# Il wizard guida da zero: prerequisiti, .env ottimizzato, servizio NSSM (o alternative).
#
# Opzioni avanzate:
#   -SkipWizard       salta prompt iniziale (mantiene wizard configurazione se .env assente)
#   -SkipTools        non installa Python/Git/Poetry/ffmpeg via winget
#   -SkipDeps         non esegue poetry install
#   -SkipModel        non scarica il modello detection
#   -SkipFfmpeg       non installa ffmpeg
#   -SkipConfig       non esegue il wizard .env
#   -SkipService      non configura NSSM / Task Scheduler
#   -ForceConfig      rigenera .env (attenzione: sovrascrive configurazione)
#   -ServiceMode nssm|task|manual   forza modalità servizio
#   -OpenFirewall     apre porta APP_PORT nel firewall (richiede admin)
#   -Run              avvio manuale al termine (solo con -ServiceMode manual)

[CmdletBinding()]
param(
    [switch]$SkipWizard,
    [switch]$SkipTools,
    [switch]$SkipDeps,
    [switch]$SkipModel,
    [switch]$SkipFfmpeg,
    [switch]$SkipConfig,
    [switch]$SkipService,
    [switch]$ForceConfig,
    [ValidateSet("nssm", "task", "manual", "")]
    [string]$ServiceMode = "",
    [switch]$OpenFirewall,
    [switch]$Run,
    [int]$AppPort = 0
)

$ErrorActionPreference = "Stop"
$AppName = "BLACKFRAME"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

$script:Py = $null
$script:Poetry = $null
$script:WizardServiceMode = ""
$script:WizardAppPort = 8000
$script:WizardLanAccess = $false

function Write-Step([int]$Number, [int]$Total, [string]$Message) {
    Write-Host ""
    Write-Host "[$AppName] Passo $Number/$Total — $Message" -ForegroundColor Cyan
}

function Write-Log([string]$Message) {
    Write-Host "  $Message"
}

function Write-Warn([string]$Message) {
    Write-Host ""
    Write-Host "[$AppName] ATTENZIONE: $Message" -ForegroundColor Yellow
}

function Write-Ok([string]$Message) {
    Write-Host "  OK — $Message" -ForegroundColor Green
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-IsAdmin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Read-YesNo([string]$Prompt, [bool]$Default = $true) {
    $suffix = if ($Default) { "[S/n]" } else { "[s/N]" }
    while ($true) {
        $answer = Read-Host "$Prompt $suffix"
        if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
        switch ($answer.Trim().ToLower()) {
            { $_ -in "s", "si", "y", "yes" } { return $true }
            { $_ -in "n", "no" } { return $false }
        }
        Write-Host "  Rispondi s o n."
    }
}

function Ensure-WingetPackage {
    param([string]$Id, [string]$Label)
    if (-not (Test-Command winget)) {
        Write-Warn "winget non disponibile: installa manualmente $Label"
        return
    }
    Write-Log "Installo/verifico $Label ($Id)"
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
        @("py", @("-3.13")), @("py", @("-3.12")), @("py", @("-3.11")),
        @("python", @()), @("python3", @())
    )
    foreach ($item in $candidates) {
        $cmd = $item[0]; $args = $item[1]
        if (-not (Test-Command $cmd)) { continue }
        & $cmd @args -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return @{ Command = $cmd; Args = $args } }
    }
    return $null
}

function Invoke-Python {
    param([string[]]$ScriptArgs)
    if ($script:Py.Command -eq "py") { & py @($script:Py.Args + $ScriptArgs) }
    else { & $script:Py.Command @ScriptArgs }
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
                "Path", ($userPath.TrimEnd(";") + ";" + $scriptsDir).TrimStart(";"), "User"
            )
        }
    }
    Refresh-PathFromRegistry
}

function Install-ProjectDeps {
    Write-Log "poetry install --with windows"
    & $script:Poetry install --with windows
    & $script:Poetry run python -c "import cv2, flask, dotenv, cryptography, waitress; print('OK')"
}

function Install-DetectionModel {
    Write-Log "Scarico modello MobileNet-SSD (~180 MB, una tantum)"
    & $script:Poetry run python scripts/fetch_model.py
}

function Write-Launcher {
    $bat = @"
@echo off
cd /d "$ProjectDir"
poetry run python scripts\check_prerequisites.py
if errorlevel 1 (
  echo.
  echo [BLACKFRAME] Prerequisiti mancanti. Vedi blackframe.log
  pause
  exit /b 1
)
poetry run python deploy\serve_waitress.py >> "$ProjectDir\blackframe.log" 2>&1
"@
    Set-Content -Path (Join-Path $ProjectDir "start_blackframe.bat") -Value $bat -Encoding ASCII
    Write-Ok "Creato start_blackframe.bat"
}

function Ensure-WindowsEnvExample {
    & $script:Poetry run python scripts/env_profiles.py --write-windows-example
}

function Invoke-ConfigWizard {
    $wizardArgs = @("scripts/windows_wizard.py", "--root", $ProjectDir)
    if ($ForceConfig) { $wizardArgs += "--force" }
    if ($ServiceMode) { $wizardArgs += @("--service-mode", $ServiceMode) }
    $output = & $script:Poetry run python @wizardArgs 2>&1 | Tee-Object -Variable wizardOut
    $text = ($wizardOut | Out-String)
    if ($text -match '__SERVICE_MODE__=(\w+)') { $script:WizardServiceMode = $Matches[1] }
    if ($text -match '__APP_PORT__=(\d+)') { $script:WizardAppPort = [int]$Matches[1] }
    if ($text -match '__LAN_ACCESS__=(true|false)') { $script:WizardLanAccess = ($Matches[1] -eq "true") }
    if ($LASTEXITCODE -ne 0) { throw "Wizard configurazione fallito." }
}

function Get-AppPort {
    if ($AppPort -gt 0) { return $AppPort }
    if ($script:WizardAppPort -gt 0) { return $script:WizardAppPort }
    if (Test-Path ".env") {
        $line = Select-String -Path ".env" -Pattern '^\s*APP_PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($line) { return [int]$line.Matches.Groups[1].Value }
    }
    return 8000
}

function Open-FirewallPort {
    param([int]$Port)
    Write-Log "Apro porta TCP $Port nel firewall (profilo Privato)"
    & $script:Poetry run python scripts/windows_service.py open-firewall --port $Port --root $ProjectDir | Out-Null
}

function Stop-DuplicateListeners {
    param([int]$Port)
    $statusJson = & $script:Poetry run python scripts/windows_service.py status --root $ProjectDir
    $status = $statusJson | ConvertFrom-Json
    $listeners = @($status.listeners)
    if ($listeners.Count -le 1) { return }
    Write-Warn "Trovati $($listeners.Count) processi in ascolto sulla porta $Port."
    foreach ($listener in $listeners) {
        $pid = [int]$listener.pid
        try {
            $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -eq "python") {
                Write-Log "Termino processo duplicato PID $pid"
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }
}

function Install-Service {
    param([string]$Mode, [int]$Port)
    switch ($Mode) {
        "nssm" {
            if (-not (Test-IsAdmin)) {
                Write-Warn "NSSM richiede PowerShell come Amministratore."
                Write-Host @"

  Apri PowerShell come amministratore ed esegui:
    cd `"$ProjectDir`"
    .\scripts\install_windows.ps1 -SkipWizard -SkipTools -SkipDeps -SkipModel -SkipConfig -ServiceMode nssm

"@
                return
            }
            Stop-DuplicateListeners -Port $Port
            Write-Log "Registro servizio Windows con NSSM"
            $result = & $script:Poetry run python scripts/windows_service.py install-nssm --port $Port --root $ProjectDir
            Write-Host $result
            Write-Ok "Servizio BLACKFRAME registrato (avvio automatico al boot)"
        }
        "task" {
            Write-Log "Registro attività pianificata all'avvio"
            $result = & $script:Poetry run python scripts/windows_service.py install-task --root $ProjectDir
            Write-Host $result
            Write-Ok "Task Scheduler configurato (start_blackframe.bat)"
            Write-Warn "Il Task Scheduler non riavvia l'app automaticamente in caso di crash."
        }
        "manual" {
            Write-Log "Nessun servizio automatico. Avvio manuale:"
            Write-Host "  poetry run python deploy\serve_waitress.py"
            Write-Host "  oppure .\start_blackframe.bat"
        }
    }
}

function Test-FinalHealth {
    param([int]$Port)
    Start-Sleep -Seconds 3
    $health = & $script:Poetry run python scripts/windows_service.py health --port $Port --root $ProjectDir
    $parsed = $health | ConvertFrom-Json
    if ($parsed.ok) {
        Write-Ok "Health check OK — http://127.0.0.1:$Port/health"
    } else {
        Write-Warn "L'app non risponde ancora. Controlla blackframe.log"
    }
}

function Show-Welcome {
    if ($SkipWizard) { return }
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  BLACKFRAME — Wizard installazione" -ForegroundColor Cyan
    Write-Host "  Mini PC Windows (da zero)" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Questo wizard installerà e configurerà:"
    Write-Host "  • Python, Git, FFmpeg, Poetry (se mancanti)"
    Write-Host "  • Dipendenze e modello classificazione persona/pet"
    Write-Host "  • File .env con tuning ottimizzato per mini PC"
    Write-Host "  • Servizio sempre attivo (NSSM consigliato)"
    Write-Host ""
    Write-Host "Prima di iniziare prepara:"
    Write-Host "  • IP telecamera Tapo e account RTSP (app Tapo → Avanzate)"
    Write-Host "  • Password per l'accesso all'interfaccia web"
    Write-Host ""
    if (-not (Read-YesNo "Procedere con l'installazione guidata?" $true)) {
        throw "Installazione annullata dall'utente."
    }
}

# --- main ---
$TotalSteps = 8
Show-Welcome

Write-Step 1 $TotalSteps "Strumenti di sistema"
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
} else {
    Write-Log "Skip installazione strumenti (-SkipTools)"
}

$script:Py = Find-Python
if (-not $script:Py) { throw "Python 3.11+ non trovato. Installa Python e riapri PowerShell." }
Write-Ok ("Python " + (Invoke-Python @("-c", "import sys; print(sys.version.split()[0])")))

if (-not (Test-Command git)) { throw "Git non trovato." }
Write-Ok (git --version)

$script:Poetry = Find-Poetry
if (-not $script:Poetry -and -not $SkipTools) {
    Install-Poetry
    $script:Poetry = Find-Poetry
}
if (-not $script:Poetry) { throw "Poetry non trovato." }
Write-Ok (& $script:Poetry --version)

Write-Step 2 $TotalSteps "Dipendenze Python"
if (-not $SkipDeps) { Install-ProjectDeps } else { Write-Log "Skip (-SkipDeps)" }

Write-Step 3 $TotalSteps "Template .env ottimizzato mini PC"
Ensure-WindowsEnvExample
Write-Ok "Generato .env.windows-minipc.example (copia = tuning pronto)"

Write-Step 4 $TotalSteps "Modello classificazione"
if (-not $SkipModel) { Install-DetectionModel } else { Write-Log "Skip (-SkipModel)" }

Write-Step 5 $TotalSteps "Configurazione guidata (.env)"
if (-not $SkipConfig) {
    if ((Test-Path ".env") -and -not $ForceConfig) {
        Write-Log ".env già presente — mantengo configurazione esistente"
        if (-not $ServiceMode) { $script:WizardServiceMode = "nssm" }
    } else {
        Invoke-ConfigWizard
    }
} else {
    Write-Log "Skip (-SkipConfig)"
    if (Test-Path ".env") {
        & $script:Poetry run python scripts/env_profiles.py --profile mini-pc-windows
    }
}

Write-Launcher

Write-Step 6 $TotalSteps "Verifica prerequisiti"
& $script:Poetry run python scripts/check_prerequisites.py

$resolvedPort = Get-AppPort
$resolvedServiceMode = if ($ServiceMode) { $ServiceMode } elseif ($script:WizardServiceMode) { $script:WizardServiceMode } else { "nssm" }

Write-Step 7 $TotalSteps "Servizio sempre attivo"
if (-not $SkipService) {
    if ($OpenFirewall -or $script:WizardLanAccess) {
        if (Test-IsAdmin) {
            Open-FirewallPort -Port $resolvedPort
        } else {
            Write-Warn "Firewall: riesegui come amministratore con -OpenFirewall oppure apri manualmente la porta $resolvedPort"
        }
    }
    Install-Service -Mode $resolvedServiceMode -Port $resolvedPort
} else {
    Write-Log "Skip (-SkipService)"
}

Write-Step 8 $TotalSteps "Verifica finale"
if ($resolvedServiceMode -ne "manual") {
    Test-FinalHealth -Port $resolvedPort
}

Write-Host ""
Write-Host "[$AppName] Installazione completata." -ForegroundColor Green
Write-Host ""
Write-Host "Interfaccia web: http://127.0.0.1:$resolvedPort"
Write-Host "Log servizio:    $ProjectDir\blackframe.log"
Write-Host ""
Write-Host "Comandi utili (senza make):"
Write-Host "  .\blackframe.ps1 help"
Write-Host "  .\blackframe.ps1 check-prerequisites"
Write-Host "  nssm restart BLACKFRAME     # riavvia servizio"
Write-Host "  nssm stop BLACKFRAME        # ferma servizio"
Write-Host ""
Write-Host "Nota: dopo l'installazione di ffmpeg chiudi e riapri PowerShell,"
Write-Host "      poi verifica con: ffmpeg -version"

if ($Run -and $resolvedServiceMode -eq "manual") {
    if (-not (Test-Path ".env")) { throw ".env mancante: riesegui senza -SkipConfig" }
    Write-Log "Avvio manuale (Ctrl+C per fermare)"
    & $script:Poetry run python deploy/serve_waitress.py
}
