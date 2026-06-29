# Rimuove servizio NSSM prima della disinstallazione (i dati in ProgramData restano).
$ErrorActionPreference = "SilentlyContinue"
$nssmPaths = @(
    "C:\Tools\nssm\nssm.exe",
    (Join-Path $env:ProgramFiles "nssm\nssm.exe")
)
$cmd = Get-Command nssm -ErrorAction SilentlyContinue
if ($cmd) { $nssmPaths = @($cmd.Source) + $nssmPaths }
foreach ($nssm in $nssmPaths | Select-Object -Unique) {
    if (Test-Path $nssm) {
        & $nssm stop BLACKFRAME
        & $nssm remove BLACKFRAME confirm
        break
    }
}
