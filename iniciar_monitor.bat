@echo off
cd /d "%~dp0"
title Monitor Al Aire
:loop
python monitor_aire.py
echo El monitor se detuvo, reiniciando en 10 segundos...
timeout /t 10 /nobreak >nul
goto loop
