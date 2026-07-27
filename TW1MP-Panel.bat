@echo off
setlocal
title TW1MP Panel
rem =====================================================================
rem  TW1MP admin panel (Marco): opens an SSH tunnel to the server's
rem  localhost-only web API and launches the dashboard in the browser.
rem  Keep this window open while using the panel; closing it (or Ctrl+C)
rem  closes the tunnel. Needs the SSH key at %USERPROFILE%\.ssh\tw1mp_vps.
rem =====================================================================
set "KEY=%USERPROFILE%\.ssh\tw1mp_vps"
set "SRV=root@87.106.168.34"
set "LPORT=17071"
set "RPORT=17071"

echo.
echo   TW1MP Panel - oeffne SSH-Tunnel...
echo   Dashboard: http://localhost:%LPORT%/
echo   Fenster offen lassen. Beenden: Fenster schliessen oder Strg+C.
echo.
start "" "http://localhost:%LPORT%/"
ssh -i "%KEY%" -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L %LPORT%:127.0.0.1:%RPORT% %SRV%
echo.
echo   Tunnel beendet. Fenster kann geschlossen werden.
pause
