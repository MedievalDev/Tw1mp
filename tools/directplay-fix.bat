@echo off
rem Two Worlds 1 multiplayer fix: repairs the Windows DirectPlay component
rem (dpnet.dll crash on session start). Self-elevates via UAC on double-click
rem and runs directplay-fix.ps1 from the same folder.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Fordere Adminrechte an - im Dialog bitte "Ja" klicken...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0directplay-fix.ps1"
echo.
echo Fertig. Log: %~dp0directplay-fix-log.txt
pause
