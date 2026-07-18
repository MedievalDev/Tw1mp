@echo off
title TW1MP server setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0add-server.ps1" %*
pause
