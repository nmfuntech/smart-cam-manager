# BLACKFRAME - safe in-place update for the Windows mini PC.
#
# Normally launched from the Mac with `make deploy` (scripts/deploy_minipc.sh),
# which uploads the latest copy of this script and runs it over SSH. It can also
# be run by hand from an elevated PowerShell:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\update.ps1 -Ref main
#
# Steps (any failure before the restart leaves the running service untouched;
# a failure after the restart rolls back to the previous commit):
#   1. refuse to run if tracked files have local edits
#   2. git fetch + checkout of -Ref (branch, tag or commit)
#   3. poetry sync only when pyproject.toml / poetry.lock changed
#   4. run the test suite in a separate git worktree (never in the live folder)
#   5. Windows-only setup: logs\ folder, NSSM log paths, old log cleanup
#   6. restart the service and wait for /health to report the new version
#   7. on failure: checkout the previous commit, re-sync deps, restart
#
# Keep this file ASCII-only: Windows PowerShell 5.1 misreads UTF-8 without BOM.

[CmdletBinding()]
param(
    [string]$Ref = "main",
    [string]$RepoDir = "",
    [string]$ServiceName = "BLACKFRAME",
    [switch]$SkipTests,
    [switch]$NoRestart,
    [int]$HealthTimeoutSec = 120
)

$ErrorActionPreference = "Continue"
if (-not $RepoDir) {
    $RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..") -ErrorAction SilentlyContinue).Path
}
if (-not $RepoDir -or -not (Test-Path (Join-Path $RepoDir ".git"))) {
    $RepoDir = "C:\Users\nikom\smart-cam-manager"
}
Set-Location $RepoDir

$LogDir = Join-Path $RepoDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$DeployLog = Join-Path $LogDir "deploy.log"

function Write-Step([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $DeployLog -Value $line -Encoding ASCII
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$AllowFail)
    $output = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    if ($output) { $output | ForEach-Object { Write-Host "    $_" } }
    if ($code -ne 0 -and -not $AllowFail) {
        throw ("{0} {1} failed (exit {2})" -f $Exe, ($Arguments -join " "), $code)
    }
    return $code
}

function Get-GitOutput([string[]]$Arguments) {
    $out = & git @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { return "" }
    return ($out -join "`n").Trim()
}

function Find-Poetry {
    $cmd = Get-Command poetry -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @(
        (Join-Path $env:APPDATA "Python\Scripts\poetry.exe"),
        "C:\Users\nikom\AppData\Roaming\Python\Scripts\poetry.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

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

function Sync-Dependencies([string]$FromSha, [string]$ToSha) {
    $changed = Get-GitOutput @("diff", "--name-only", $FromSha, $ToSha, "--", "pyproject.toml", "poetry.lock")
    if (-not $changed -and (Test-Path (Join-Path $RepoDir ".venv\Scripts\python.exe"))) {
        Write-Step "Dependencies unchanged, skipping poetry sync"
        return
    }
    $poetry = Find-Poetry
    if (-not $poetry) { throw "Poetry not found" }
    Write-Step "Dependencies changed: poetry sync --with windows"
    $env:POETRY_VIRTUALENVS_IN_PROJECT = "true"
    Invoke-Native $poetry @("sync", "--with", "windows", "--no-interaction", "--no-ansi") | Out-Null
}

function Ensure-Models {
    # The YOLOX detector model is not in git: download it when the backend
    # needs it (checksum-verified by scripts\fetch_model.py).
    $envFile = Join-Path $RepoDir ".env"
    $backend = ""
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^\s*CLASSIFICATION_BACKEND\s*=\s*(\S+)' | Select-Object -First 1
        if ($line) { $backend = $line.Matches[0].Groups[1].Value.Trim().ToLower() }
    }
    if ($backend -ne "yolox") { return }
    if (Test-Path (Join-Path $RepoDir "models\yolox_tiny.onnx")) { return }
    Write-Step "Downloading YOLOX-Tiny model"
    $python = Join-Path $RepoDir ".venv\Scripts\python.exe"
    Invoke-Native $python @("scripts\fetch_model.py") -AllowFail | Out-Null
}

function Invoke-TestsInWorktree([string]$Sha) {
    $worktree = Join-Path (Split-Path $RepoDir -Parent) "bf-deploy-test"
    if (Test-Path $worktree) {
        Invoke-Native git @("worktree", "remove", "--force", $worktree) -AllowFail | Out-Null
        Remove-Item -Recurse -Force $worktree -ErrorAction SilentlyContinue
    }
    Invoke-Native git @("worktree", "prune") -AllowFail | Out-Null
    Invoke-Native git @("worktree", "add", "--detach", $worktree, $Sha) | Out-Null
    $python = Join-Path $RepoDir ".venv\Scripts\python.exe"
    Write-Step "Running test suite in $worktree"
    Push-Location $worktree
    try {
        $code = Invoke-Native $python @("-m", "pytest", "-q", "-x", "-p", "no:cacheprovider") -AllowFail
    } finally {
        Pop-Location
        Invoke-Native git @("worktree", "remove", "--force", $worktree) -AllowFail | Out-Null
    }
    return ($code -eq 0)
}

function Set-WindowsServiceLogging {
    $nssm = Find-Nssm
    if (-not $nssm) {
        Write-Step "NSSM not found: skipping service log configuration"
        return
    }
    $logFile = Join-Path $LogDir "blackframe.log"
    $current = (& $nssm get $ServiceName AppStdout 2>$null | Out-String).Trim() -replace "`0", ""
    if ($current -ne $logFile) {
        Write-Step "NSSM: logs -> $logFile (rotated at each start)"
        & $nssm set $ServiceName AppStdout $logFile | Out-Null
        & $nssm set $ServiceName AppStderr $logFile | Out-Null
    }
    # Rotation only at service start. NEVER enable AppRotateOnline: in NSSM 2.24
    # the online rotation thread can stop draining the app's stdout pipe, which
    # blocks every log call (and so every HTTP request) and then hangs the
    # service in STOP_PENDING. Size-based rotation is done by the app instead.
    & $nssm set $ServiceName AppRotateFiles 1 | Out-Null
    & $nssm set $ServiceName AppRotateOnline 0 | Out-Null
    & $nssm set $ServiceName AppRotateBytes 10485760 | Out-Null

    # The app writes its own size-rotated log; NSSM's file keeps warnings/crashes.
    $envFile = Join-Path $RepoDir ".env"
    if ((Test-Path $envFile) -and -not (Select-String -Path $envFile -Pattern '^\s*LOG_FILE\s*=' -Quiet)) {
        Add-Content -Path $envFile -Value "`r`n# Log applicativo con rotazione per dimensione (aggiunto da update.ps1)`r`nLOG_FILE=logs/app.log" -Encoding ASCII
        Write-Step "Added LOG_FILE=logs/app.log to .env"
    }

    # One-off: move the logs that NSSM used to rotate into the repo root.
    $archive = Join-Path $LogDir "archive"
    $rootLogs = Get-ChildItem -Path $RepoDir -Filter "blackframe*.log" -File -ErrorAction SilentlyContinue
    if ($rootLogs) {
        New-Item -ItemType Directory -Force -Path $archive | Out-Null
        foreach ($file in $rootLogs) {
            Move-Item -Path $file.FullName -Destination $archive -Force -ErrorAction SilentlyContinue
        }
        Write-Step ("Moved {0} old log files to logs\archive" -f $rootLogs.Count)
    }
    # Keep the 30 most recent rotated logs.
    Get-ChildItem -Path $LogDir, $archive -Filter "blackframe-*.log" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Wait-Healthy([string]$ExpectedSha) {
    $port = Read-AppPort
    $url = "http://127.0.0.1:$port/health"
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    $short = $ExpectedSha.Substring(0, 7)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $svc -or $svc.Status -ne "Running") { continue }
        try {
            $resp = Invoke-RestMethod -Uri $url -TimeoutSec 5
            if ($resp.status -eq "ok" -and ((-not $resp.version) -or $resp.version.StartsWith($short))) {
                Write-Step ("Healthy: status={0} version={1}" -f $resp.status, $resp.version)
                return $true
            }
        } catch {
            continue
        }
    }
    return $false
}

function Restart-App {
    # Bounded restart: a hung stop must not hang the deploy (and the rollback).
    $nssm = Find-Nssm
    if ($nssm) {
        $proc = Start-Process -FilePath $nssm -ArgumentList @("restart", $ServiceName) -PassThru -WindowStyle Hidden
    } else {
        $proc = Start-Process -FilePath "sc.exe" -ArgumentList @("stop", $ServiceName) -PassThru -WindowStyle Hidden
    }
    if (-not $proc.WaitForExit(90000)) {
        Write-Step "WARN: restart command still running after 90 s (service stuck stopping?)"
        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($svc) { Write-Step ("Service status: {0}" -f $svc.Status) }
        return $false
    }
    if (-not $nssm) { Start-Service -Name $ServiceName -ErrorAction SilentlyContinue }
    return $true
}

function Checkout-Ref([string]$Target) {
    $remoteBranch = Get-GitOutput @("rev-parse", "--verify", "--quiet", "refs/remotes/origin/$Target")
    if ($remoteBranch) {
        $localBranch = Get-GitOutput @("rev-parse", "--verify", "--quiet", "refs/heads/$Target")
        if ($localBranch) {
            $ahead = Get-GitOutput @("rev-list", "--count", "origin/$Target..$Target")
            if ($ahead -and [int]$ahead -gt 0) {
                throw "Local branch $Target has $ahead commit(s) not on origin: push or drop them first"
            }
        }
        Invoke-Native git @("checkout", "-q", "-B", $Target, "origin/$Target") | Out-Null
    } else {
        Invoke-Native git @("checkout", "-q", "--detach", $Target) | Out-Null
    }
}

# ---------------------------------------------------------------------------
Write-Step "=== BLACKFRAME update to '$Ref' ==="

$dirty = Get-GitOutput @("status", "--porcelain", "--untracked-files=no")
if ($dirty) {
    Write-Step "ABORT: tracked files modified on the mini PC:"
    Write-Host $dirty
    exit 2
}

$prevSha = Get-GitOutput @("rev-parse", "HEAD")
$prevBranch = Get-GitOutput @("symbolic-ref", "--short", "-q", "HEAD")
$prevRef = $prevSha
if ($prevBranch) { $prevRef = $prevBranch }
Write-Step "Current: $prevRef ($prevSha)"

try {
    Invoke-Native git @("fetch", "--prune", "--quiet", "origin") | Out-Null
    $newSha = Get-GitOutput @("rev-parse", "--verify", "--quiet", "origin/$Ref^{commit}")
    if (-not $newSha) { $newSha = Get-GitOutput @("rev-parse", "--verify", "--quiet", "$Ref^{commit}") }
    if (-not $newSha) { throw "Unknown ref: $Ref" }
    Write-Step "Target:  $Ref ($newSha)"

    Checkout-Ref $Ref
    Sync-Dependencies $prevSha $newSha
    Ensure-Models

    if (-not $SkipTests) {
        if (-not (Invoke-TestsInWorktree $newSha)) {
            throw "Test suite failed"
        }
        Write-Step "Tests passed"
    }
} catch {
    Write-Step "ABORT before restart: $($_.Exception.Message)"
    Write-Step "Restoring $prevRef"
    Invoke-Native git @("checkout", "-q", $prevRef) -AllowFail | Out-Null
    try { Sync-Dependencies $newSha $prevSha } catch { Write-Step "WARN: dependency restore failed: $_" }
    exit 3
}

Set-WindowsServiceLogging

if ($NoRestart) {
    Write-Step "Code updated, restart skipped (-NoRestart)"
    exit 0
}

Write-Step "Restarting service $ServiceName"
$restarted = Restart-App
if ($restarted -and (Wait-Healthy $newSha)) {
    Write-Step "=== Update OK: $prevSha -> $newSha ==="
    exit 0
}

Write-Step "Service not healthy within $HealthTimeoutSec s: ROLLBACK to $prevRef"
Invoke-Native git @("checkout", "-q", $prevRef) -AllowFail | Out-Null
try { Sync-Dependencies $newSha $prevSha } catch { Write-Step "WARN: dependency restore failed: $_" }
$restarted = Restart-App
if ($restarted -and (Wait-Healthy $prevSha)) {
    Write-Step "Rollback OK: running $prevSha again"
} else {
    Write-Step "CRITICAL: service not healthy after rollback, check logs\blackframe.log"
}
exit 4
