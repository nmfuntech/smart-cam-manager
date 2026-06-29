# Riavvia il servizio Windows BLACKFRAME (richiede esecuzione come amministratore).
param(
    [string]$ServiceName = "BLACKFRAME",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"

function Find-NssmPath {
    $inPath = Get-Command nssm -ErrorAction SilentlyContinue
    if ($inPath) {
        return $inPath.Source
    }
    foreach ($candidate in @("C:\Tools\nssm\nssm.exe", "C:\nssm\nssm.exe")) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

function Read-AppPort {
    $root = Split-Path $PSScriptRoot -Parent
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path $envFile)) {
        return 8000
    }
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        if ($line -match '^\s*APP_PORT\s*=\s*(\d+)\s*$') {
            return [int]$Matches[1]
        }
    }
    return 8000
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host ""
Write-Host "BLACKFRAME — riavvio servizio" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-IsAdmin)) {
    Write-Host "Esegui come amministratore (doppio clic su restart-blackframe.bat)." -ForegroundColor Red
    if (-not $NoPause) {
        Read-Host "Premi Invio per chiudere"
    }
    exit 1
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Host "Servizio '$ServiceName' non trovato." -ForegroundColor Red
    Write-Host "Installa con: .\blackframe.ps1 install-windows"
    if (-not $NoPause) {
        Read-Host "Premi Invio per chiudere"
    }
    exit 1
}

Write-Host "Stato attuale: $($service.Status)"
Write-Host "Riavvio in corso..."

$nssm = Find-NssmPath
if ($nssm) {
    Write-Host "NSSM: $nssm"
    & $nssm restart $ServiceName
    if ($LASTEXITCODE -ne 0) {
        throw "nssm restart fallito (codice $LASTEXITCODE)"
    }
} else {
    Write-Host "NSSM non trovato, uso Restart-Service."
    Restart-Service -Name $ServiceName -Force
}

Start-Sleep -Seconds 3

$service.Refresh()
Write-Host ""
if ($service.Status -eq "Running") {
    Write-Host "Servizio riavviato: RUNNING" -ForegroundColor Green
} else {
    Write-Host "Servizio non in esecuzione: $($service.Status)" -ForegroundColor Yellow
}

$port = Read-AppPort
$healthUrl = "http://127.0.0.1:$port/health"
Write-Host "Verifica health: $healthUrl"
try {
    $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 10
    if ($response.Content -match '"status"\s*:\s*"ok"') {
        Write-Host "Health check: OK" -ForegroundColor Green
        Write-Host "Interfaccia: http://127.0.0.1:$port"
    } else {
        Write-Host "Health check: risposta inattesa" -ForegroundColor Yellow
    }
} catch {
    Write-Host "Health check non riuscito (il servizio potrebbe essere ancora in avvio)." -ForegroundColor Yellow
    Write-Host $_.Exception.Message
}

Write-Host ""
if (-not $NoPause) {
    Read-Host "Premi Invio per chiudere"
}
