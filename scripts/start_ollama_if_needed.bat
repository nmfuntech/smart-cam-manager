@echo off
setlocal
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
set "MODEL=qwen2.5:0.5b"

netstat -ano | findstr /C:"127.0.0.1:11434" | findstr /C:"LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    start "" /B "%OLLAMA%" serve
    timeout /t 3 /nobreak >nul
)

REM Precarica il modello agente in RAM (cold start >8s su mini PC).
echo. | "%OLLAMA%" run %MODEL% --keepalive 24h >nul 2>&1
