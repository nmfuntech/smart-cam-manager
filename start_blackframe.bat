@echo off
cd /d "C:\Users\nikom\smart-cam-manager"
poetry run python scripts\check_prerequisites.py
if errorlevel 1 (
  echo.
  echo [BLACKFRAME] Prerequisiti mancanti. Vedi messaggi sopra.
  echo Installa ffmpeg: winget install Gyan.FFmpeg
  echo Poi riapri il terminale e rilancia.
  pause
  exit /b 1
)
poetry run python deploy\serve_waitress.py >> "C:\Users\nikom\smart-cam-manager\blackframe.log" 2>&1
