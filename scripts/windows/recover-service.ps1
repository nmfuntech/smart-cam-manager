# BLACKFRAME - unblock a service stuck in STOP_PENDING and bring it back up.
#
# Run from an ELEVATED PowerShell on the mini PC:
#   .\blackframe.ps1 recover
#
# 1. if the service is stuck stopping, terminate its NSSM host process
# 2. make sure NSSM online log rotation is off (it caused the hang)
# 3. start the service and wait for /health
# 4. if it does not come up healthy, check out the last known-good commit
#    (-FallbackRef) and start again
#
# Keep this file ASCII-only (Windows PowerShell 5.1).

[CmdletBinding()]
param(
    [string]$ServiceName = "BLACKFRAME",
    [string]$FallbackRef = "3c52bd7",
    [int]$HealthTimeoutSec = 120
)

$ErrorActionPreference = "Continue"
$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoDir

function Find-Nssm {
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @("C:\Tools\nssm\nssm.exe", "C:\nssm\nssm.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Read-AppPort {
    $envFile = Join-Path $RepoDir ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*APP_PORT\s*=\s*(\d+)\s*$') { return [int]$Matches[1] }
        }
    }
    return 8000
}

function Wait-Healthy {
    $url = "http://127.0.0.1:{0}/health" -f (Read-AppPort)
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        try {
            $resp = Invoke-RestMethod -Uri $url -TimeoutSec 5
            if ($resp.status -eq "ok") {
                Write-Host ("Healthy: version {0}" -f $resp.version)
                return $true
            }
        } catch { continue }
    }
    return $false
}

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this from an elevated (administrator) PowerShell." -ForegroundColor Red
    exit 1
}

$nssm = Find-Nssm
$svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if (-not $svc) {
    Write-Host "Service $ServiceName not found." -ForegroundColor Red
    exit 1
}
Write-Host ("Service state: {0} (pid {1})" -f $svc.State, $svc.ProcessId)

if ($svc.State -eq "Stop Pending" -and $svc.ProcessId -gt 0) {
    Write-Host ("Terminating stuck service host pid {0}" -f $svc.ProcessId)
    Stop-Process -Id $svc.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}
# Leftover NSSM hosts that are no longer the service process.
$current = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").ProcessId
Get-CimInstance Win32_Process -Filter "Name='nssm.exe'" |
    Where-Object { $_.CommandLine -notmatch ' (restart|start|stop|set|get) ' -and $_.ProcessId -ne $current } |
    ForEach-Object {
        Write-Host ("Terminating orphan NSSM host pid {0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

if ($nssm) { & $nssm set $ServiceName AppRotateOnline 0 | Out-Null }

Write-Host "Starting $ServiceName"
Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (Wait-Healthy) {
    Write-Host "BLACKFRAME is running." -ForegroundColor Green
    exit 0
}

Write-Host "Not healthy: falling back to $FallbackRef" -ForegroundColor Yellow
Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
& git checkout -q $FallbackRef
Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (Wait-Healthy) {
    Write-Host "Running the fallback version. Check logs\app.log and tell Claude." -ForegroundColor Yellow
    exit 4
}
Write-Host "Service still not healthy: check logs\blackframe.log" -ForegroundColor Red
exit 5
