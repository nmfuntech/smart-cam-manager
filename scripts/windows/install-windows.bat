@echo off
REM Wizard installazione BLACKFRAME su Windows (doppio clic o da cmd)
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\install_windows.ps1" %*
