@echo off
setlocal EnableExtensions

:: Richiede privilegi amministratore (necessari per gestire il servizio Windows).
net session >nul 2>&1
if errorlevel 1 (
  echo [BLACKFRAME] Richiesta elevazione amministratore...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "SERVICE=BLACKFRAME"
set "NSSM="

where nssm >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('where nssm 2^>nul') do (
    set "NSSM=%%I"
    goto :found_nssm
  )
)

if exist "C:\Tools\nssm\nssm.exe" set "NSSM=C:\Tools\nssm\nssm.exe"

:found_nssm
if not defined NSSM (
  echo [BLACKFRAME] nssm.exe non trovato. Installa NSSM o aggiungilo al PATH.
  echo Esempio: C:\Tools\nssm\nssm.exe
  pause
  exit /b 1
)

echo [BLACKFRAME] Riavvio servizio %SERVICE%...
"%NSSM%" restart %SERVICE%
if errorlevel 1 (
  echo [BLACKFRAME] Riavvio fallito.
  pause
  exit /b 1
)

echo.
sc query %SERVICE%
echo.
echo Verifica: http://localhost:8000/health
pause
