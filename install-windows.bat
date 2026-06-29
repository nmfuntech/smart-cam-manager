@echo off
REM Wizard installazione BLACKFRAME su Windows (doppio clic o da cmd)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_windows.ps1" %*
