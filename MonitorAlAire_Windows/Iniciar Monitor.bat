@echo off
cd /d "%~dp0"
title Monitor Al Aire
powershell -NoProfile -ExecutionPolicy Bypass -File "app\instalar.ps1"
if errorlevel 1 (
  echo.
  echo No se pudo preparar el programa. Revisa el mensaje de arriba.
  pause
  exit /b 1
)
start "" "python\pythonw.exe" "app\monitor_app.py"
