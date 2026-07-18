# DirectPlay repair for Two Worlds 1 multiplayer (dpnet.dll crash on the
# multiplayer session start). Enables the DirectPlay legacy component if it
# is off, otherwise repairs the component store (DISM + sfc). Runs elevated,
# logs to a file next to itself, and never reboots on its own.
$log = Join-Path $PSScriptRoot 'directplay-fix-log.txt'
Start-Transcript -Path $log -Force
try {
    Write-Output "=== DirectPlay-Fix gestartet: $(Get-Date) ==="

    $dp = Get-WindowsOptionalFeature -Online -FeatureName DirectPlay
    $lc = Get-WindowsOptionalFeature -Online -FeatureName LegacyComponents
    Write-Output "DirectPlay: $($dp.State) | LegacyComponents: $($lc.State)"

    if ($dp.State -ne 'Enabled') {
        Write-Output '-> Aktiviere DirectPlay (+LegacyComponents), ohne Neustart...'
        Enable-WindowsOptionalFeature -Online -FeatureName DirectPlay -All -NoRestart
        Write-Output '-> Aktivierung abgeschlossen. Bitte danach neu starten.'
    } else {
        Write-Output '-> DirectPlay bereits aktiv. Repariere Komponentenspeicher (dauert einige Minuten)...'
        DISM /Online /Cleanup-Image /RestoreHealth
        Write-Output '-> DISM fertig. Starte Systemdatei-Pruefung (sfc)...'
        sfc /scannow
        Write-Output '-> sfc fertig.'
    }

    Write-Output '=== Versionsstand dpnet nach Fix ==='
    foreach ($p in "$env:WINDIR\SysWOW64\dpnet.dll", "$env:WINDIR\System32\dpnet.dll") {
        if (Test-Path $p) { Write-Output "$p -> $((Get-Item $p).VersionInfo.FileVersion)" }
    }
    Write-Output "=== FERTIG: $(Get-Date) ==="
} catch {
    Write-Output "FEHLER: $_"
} finally {
    Stop-Transcript
}
