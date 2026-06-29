# Post-installazione Inno Setup: dati in ProgramData + wizard opzionale.
param(
    [switch]$SkipWizard,
    [switch]$SkipService
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $AppDir
$DataHome = Join-Path $env:ProgramData "BLACKFRAME"

function Write-Log([string]$Message) {
    Write-Host "[BLACKFRAME] $Message" -ForegroundColor Cyan
}

New-Item -ItemType Directory -Force -Path $DataHome, "$DataHome\captures", "$DataHome\data" | Out-Null
Set-Content -Path (Join-Path $DataHome ".installed") -Value (Get-Date -Format o) -Encoding ASCII

$envTemplate = Join-Path $ProjectDir ".env.windows-minipc.example"
$envTarget = Join-Path $DataHome ".env"
if ((Test-Path $envTemplate) -and -not (Test-Path $envTarget)) {
    Copy-Item $envTemplate $envTarget
    Write-Log "Creato $envTarget dal template mini PC"
}

$python = Join-Path $ProjectDir "runtime\python\python.exe"
if (-not (Test-Path $python)) {
    $python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}

if (-not (Test-Path $python)) {
    Write-Log "Runtime Python non trovato in {app}; usa install-windows per configurare."
} elseif (-not $SkipWizard) {
    Write-Log "Avvio wizard configurazione"
    $env:BLACKFRAME_HOME = $DataHome
    & $python (Join-Path $ProjectDir "scripts\windows_wizard.py") --root $ProjectDir --env-file $envTarget
}

if (-not $SkipService) {
    if (-not (Test-Path $python)) {
        Write-Log "Salto servizio: Python non trovato"
    } else {
        $env:BLACKFRAME_HOME = $DataHome
        & $python (Join-Path $ProjectDir "scripts\windows_service.py") install-nssm --root $ProjectDir
    }
}

Write-Log "Installazione completata. Dati: $DataHome"
Write-Log "Interfaccia: http://127.0.0.1:8000"
