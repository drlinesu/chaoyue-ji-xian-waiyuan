@echo off
cd /d "%~dp0anime-app"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0anime-app\launcher.ps1"
