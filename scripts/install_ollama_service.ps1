#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registra Ollama come servizio Windows (NSSM) con avvio automatico al boot.

.DESCRIPTION
    Ollama e' gia' in esecuzione se l'app tray e' aperta: in quel caso
    "ollama serve" fallisce perche' la porta 11434 e' occupata. Questo script
    ferma le istanze manuali/tray e installa un unico servizio NSSM.

.EXAMPLE
    .\scripts\install_ollama_service.ps1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ServiceName = "Ollama"
$OllamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
$OllamaDir = Split-Path $OllamaExe -Parent
$LogDir = Join-Path $env:LOCALAPPDATA "Ollama"
$LogFile = Join-Path $LogDir "ollama-service.log"
$ModelsDir = Join-Path $env:USERPROFILE ".ollama\models"
$nssmInPath = Get-Command nssm -ErrorAction SilentlyContinue
$NssmPath = @(
    $(if ($nssmInPath) { $nssmInPath.Source }),
    "C:\Tools\nssm\nssm.exe",
    "C:\nssm\nssm.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique -First 1

function Write-Step([string]$Message) {
    Write-Host ">> $Message" -ForegroundColor Cyan
}

function Invoke-Nssm([string[]]$Args) {
    & $NssmPath @Args
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($Args -join ' ') fallito (codice $LASTEXITCODE)"
    }
}

if (-not (Test-Path $OllamaExe)) {
    throw "Ollama non trovato: $OllamaExe — installalo da https://ollama.com/download"
}

if (-not $NssmPath -or -not (Test-Path $NssmPath)) {
    throw "NSSM non trovato. Installa BLACKFRAME con -ServiceMode nssm o scarica NSSM in C:\Tools\nssm\"
}

Write-Step "Fermo istanze Ollama esistenti (tray / serve manuale)..."
Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$existing = sc.exe query $ServiceName 2>&1 | Out-String
if ($existing -notmatch "1060") {
    Write-Step "Rimuovo servizio $ServiceName esistente..."
    Invoke-Nssm @("stop", $ServiceName) 2>$null
    Invoke-Nssm @("remove", $ServiceName, "confirm")
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Step "Registro servizio NSSM $ServiceName..."
Invoke-Nssm @("install", $ServiceName, $OllamaExe, "serve")
Invoke-Nssm @("set", $ServiceName, "AppDirectory", $OllamaDir)
Invoke-Nssm @("set", $ServiceName, "DisplayName", "Ollama LLM Server")
Invoke-Nssm @("set", $ServiceName, "Description", "Server LLM locale (BLACKFRAME agent)")
Invoke-Nssm @("set", $ServiceName, "Start", "SERVICE_AUTO_START")
Invoke-Nssm @("set", $ServiceName, "AppStdout", $LogFile)
Invoke-Nssm @("set", $ServiceName, "AppStderr", $LogFile)
Invoke-Nssm @("set", $ServiceName, "AppRotateFiles", "1")
Invoke-Nssm @("set", $ServiceName, "AppRotateBytes", "10485760")

$envExtra = "OLLAMA_HOST=127.0.0.1:11434`nOLLAMA_MODELS=$ModelsDir"
Invoke-Nssm @("set", $ServiceName, "AppEnvironmentExtra", $envExtra)

Write-Step "Avvio servizio $ServiceName..."
Invoke-Nssm @("start", $ServiceName)
Start-Sleep -Seconds 3

try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 10
    Write-Host "OK — Ollama risponde su http://127.0.0.1:11434" -ForegroundColor Green
    Write-Host "   Log: $LogFile"
    Write-Host "   Comandi: nssm status/restart/stop $ServiceName"
} catch {
    Write-Warning "Servizio avviato ma l'API non risponde ancora. Controlla $LogFile"
    Get-Content $LogFile -Tail 20 -ErrorAction SilentlyContinue
}
