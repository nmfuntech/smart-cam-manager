# Prepara staging e compila l'installer Inno Setup (.exe).
# Prerequisito: Inno Setup 6 — https://jrsoftware.org/isdl.php
#
# Uso:
#   .\scripts\build_windows_installer.ps1
#   .\scripts\build_windows_installer.ps1 -WithModel
#   .\blackframe.ps1 build-installer

[CmdletBinding()]
param(
    [switch]$WithModel,
    [switch]$SkipCompile,
    [switch]$SkipRuntime,
    [string]$Version = "",
    [string]$PythonVersion = "3.11.9"
)

$ErrorActionPreference = "Stop"
$AppName = "BLACKFRAME"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$DistDir = Join-Path $Root "dist"
$StagingDir = Join-Path $DistDir "blackframe-staging"
$BuildDir = Join-Path $Root "build"
$CacheDir = Join-Path $BuildDir "cache"
$IssFile = Join-Path $Root "deploy\blackframe.iss"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "[$AppName] $Message" -ForegroundColor Cyan
}

function Get-ProjectVersion {
    param([string]$Override)
    if ($Override) { return $Override }
    $pyproject = Join-Path $Root "pyproject.toml"
    $match = Select-String -Path $pyproject -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { return $match.Matches.Groups[1].Value }
    return "0.1.0"
}

function Find-ISCC {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    ) | Where-Object { $_ -and (Test-Path $_) }
    return $candidates | Select-Object -First 1
}

function Find-Poetry {
    if (Get-Command poetry -ErrorAction SilentlyContinue) { return "poetry" }
    $poetryExe = Join-Path $env:APPDATA "Python\Scripts\poetry.exe"
    if (Test-Path $poetryExe) { return $poetryExe }
    return $null
}

function Export-Requirements {
    param([string]$OutputPath)
    $poetry = Find-Poetry
    if (-not $poetry) { throw "Poetry richiesto per esportare le dipendenze." }
    & $poetry export -f requirements.txt --without-hashes --with windows -o $OutputPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  poetry export fallito, uso pip freeze dal venv di sviluppo" -ForegroundColor Yellow
        & $poetry run pip freeze | Set-Content -Path $OutputPath -Encoding UTF8
    }
}

function Ensure-EmbeddedPython {
    param(
        [string]$Version,
        [string]$TargetDir
    )
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    $zipName = "python-$Version-embed-amd64.zip"
    $zipPath = Join-Path $CacheDir $zipName
    $url = "https://www.python.org/ftp/python/$Version/$zipName"
    if (-not (Test-Path $zipPath)) {
        Write-Step "Scarico Python embed $Version"
        New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    }
    if (Test-Path $TargetDir) {
        Get-ChildItem $TargetDir -Force | Remove-Item -Recurse -Force
    }
    Expand-Archive -Path $zipPath -DestinationPath $TargetDir -Force
    $pth = Get-ChildItem $TargetDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $pth) { throw "File ._pth non trovato in Python embed." }
    $zipName = (Get-ChildItem $TargetDir -Filter "python*.zip" | Select-Object -First 1).Name
    if (-not $zipName) { $zipName = "python311.zip" }
    @(
        $zipName,
        ".",
        "..\..\Lib\site-packages",
        "import site"
    ) | Set-Content -Path $pth.FullName -Encoding ASCII
}

function Install-RuntimePackages {
    param(
        [string]$PythonExe,
        [string]$RequirementsFile,
        [string]$SitePackages
    )
    New-Item -ItemType Directory -Force -Path $SitePackages | Out-Null
    $getPip = Join-Path $CacheDir "get-pip.py"
    if (-not (Test-Path $getPip)) {
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
    }
    Write-Step "Installo pip nel runtime Python"
    & $PythonExe $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip fallito." }
    Write-Step "Installo dipendenze nel pacchetto (può richiedere alcuni minuti)"
    & $PythonExe -m pip install --no-warn-script-location -r $RequirementsFile --target $SitePackages
    if ($LASTEXITCODE -ne 0) { throw "pip install fallito." }
}

function Copy-AppSources {
    param([string]$Destination)
    if (Test-Path $Destination) {
        Remove-Item $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $excludeDirs = @(
        ".git", ".venv", "dist", "build", "captures", "__pycache__", ".pytest_cache",
        ".claude", ".idea", ".vscode", "agent-transcripts"
    )
    $excludeFiles = @("*.pyc", "*.log", ".env")
    robocopy $Root $Destination /E /NFL /NDL /NJH /NJS /nc /ns /np `
        /XD $excludeDirs `
        /XF $excludeFiles | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy fallito ($LASTEXITCODE)." }
}

# --- main ---
$AppVersion = Get-ProjectVersion -Override $Version
Write-Step "Build installer v$AppVersion"

Write-Step "1/5 - Template .env Windows"
$poetry = Find-Poetry
if ($poetry) {
    & $poetry run python scripts/env_profiles.py --write-windows-example
}

Write-Step "2/5 - Staging file applicazione"
Copy-AppSources -Destination $StagingDir
Set-Content -Path (Join-Path $StagingDir "VERSION.txt") -Value $AppVersion -Encoding ASCII

if (-not $SkipRuntime) {
    Write-Step "3/5 - Runtime Python portabile"
    $runtimePythonDir = Join-Path $StagingDir "runtime\python"
    $sitePackages = Join-Path $StagingDir "Lib\site-packages"
    Ensure-EmbeddedPython -Version $PythonVersion -TargetDir $runtimePythonDir
    $reqFile = Join-Path $BuildDir "requirements-installer.txt"
    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
    Export-Requirements -OutputPath $reqFile
    $pythonExe = Join-Path $runtimePythonDir "python.exe"
    Install-RuntimePackages -PythonExe $pythonExe -RequirementsFile $reqFile -SitePackages $sitePackages
} else {
    Write-Step "3/5 - Runtime Python (skip)"
}

if ($WithModel) {
    Write-Step "4/5 - Modello classificazione"
    $modelsDir = Join-Path $StagingDir "models"
    New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
    if ($poetry) {
        & $poetry run python scripts/fetch_model.py
        if (Test-Path (Join-Path $Root "models")) {
            robocopy (Join-Path $Root "models") $modelsDir /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
        }
    }
} else {
    Write-Step "4/5 - Modello classificazione (skip, usa -WithModel per includerlo)"
}

if (-not $SkipCompile) {
    Write-Step "5/5 - Compilazione Inno Setup"
    $iscc = Find-ISCC
    if (-not $iscc) {
        throw "Inno Setup 6 non trovato. Installa da https://jrsoftware.org/isdl.php oppure aggiungi ISCC.exe al PATH."
    }
    New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
    $defineArg = '/DAppVersion=' + $AppVersion
    & $iscc $defineArg $IssFile
    if ($LASTEXITCODE -ne 0) { throw "ISCC fallito ($LASTEXITCODE)." }
    $installer = Join-Path $DistDir "BLACKFRAME-Setup-$AppVersion.exe"
    if (Test-Path $installer) {
        Write-Host ""
        Write-Host "[$AppName] Installer creato:" -ForegroundColor Green
        Write-Host "  $installer"
    }
} else {
    Write-Step "5/5 - Compilazione Inno Setup (skip, staging in $StagingDir)"
}

Write-Host ""
Write-Host "Staging: $StagingDir"
