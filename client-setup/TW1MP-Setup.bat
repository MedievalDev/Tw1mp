@echo off
rem =====================================================================
rem  TW1MP - Two Worlds 1 community server: one-click setup for players.
rem  Double-click this file. It fetches the latest setup script from
rem  GitHub and runs it: adds the server to your game's server list and
rem  enables DirectPlay. No manual input needed.
rem =====================================================================
title TW1MP Setup
echo.
echo   TW1MP - Setup wird von GitHub geladen und ausgefuehrt...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; iex (irm 'https://raw.githubusercontent.com/MedievalDev/Tw1mp/main/client-setup/TW1MP-Setup.ps1')"
echo.
echo   Fenster kann geschlossen werden.
pause
