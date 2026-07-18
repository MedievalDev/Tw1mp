@echo off
rem Set every active network connection to the "Private" category so the
rem TW1MP Lobby firewall rule (scoped to Private/Domain) actually applies.
rem A home WLAN should be Private anyway. Self-elevates via UAC.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Fordere Adminrechte an - im Dialog bitte "Ja" klicken...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -Command "Get-NetConnectionProfile | ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }; Get-NetConnectionProfile | Select-Object Name, NetworkCategory | Format-Table -AutoSize"
echo.
echo Fertig. Alle aktiven Netzwerke stehen jetzt auf "Privat".
pause
