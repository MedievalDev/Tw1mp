@echo off
rem Allow LAN clients to reach the TW1MP lobby server (TCP 17171) and the
rem optional web status API (TCP 17071). Self-elevates via UAC on double-click.
rem The Two Worlds game itself already has inbound firewall rules from Steam;
rem this only opens the Python lobby server's port.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Fordere Adminrechte an - im Dialog bitte "Ja" klicken...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo Lege Firewall-Regeln an...
netsh advfirewall firewall delete rule name="TW1MP Lobby" >nul 2>&1
netsh advfirewall firewall add rule name="TW1MP Lobby" dir=in action=allow protocol=TCP localport=17171 profile=private,domain
netsh advfirewall firewall delete rule name="TW1MP Web" >nul 2>&1
netsh advfirewall firewall add rule name="TW1MP Web" dir=in action=allow protocol=TCP localport=17071 profile=private,domain
echo.
echo Fertig. Der Lobby-Server ist jetzt aus dem LAN erreichbar (private Netzwerke).
echo Wichtig: Das WLAN muss als "Privates Netzwerk" eingestuft sein, nicht "Oeffentlich".
pause
