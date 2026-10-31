# Activate (or remove) the DirectPlay8 relay replacement for the current user.
#
# The 32-bit game resolves the DirectPlay8 CLSIDs through the WOW6432Node
# view of the class registry; writing them under HKCU needs no admin and
# overrides the system dpnet.dll for this user only. Removing restores the
# stock Windows DirectPlay (or whatever was registered before is simply
# gone - the capture shim and the old replacement use the same keys).
#
#   .\register.ps1                          -> use the relay replacement
#   .\register.ps1 -Relay 192.168.1.136:17172 -> ... and point hosting at that relay
#   .\register.ps1 -Remove                  -> restore stock DirectPlay (and drop the relay setting)
#   .\register.ps1 -Status                  -> show what is registered + relay setting
#
# Only the HOSTING player needs the relay address (joiners get it from the
# game's session address). Lookup order in the DLL: env TW1MP_RELAY, then
# HKCU\Software\TW1MP\Relay, then the game's natresolver setting, then the
# built-in community server.
param([switch]$Remove, [switch]$Status, [string]$Relay)

$dll = Join-Path $PSScriptRoot 'dpnetrelay.dll'
$clsids = [ordered]@{
    '{286F484D-375E-4458-A272-B138E2F80A6A}' = 'DirectPlay8Peer'
    '{934A9523-A3CA-4BC5-ADA0-D6D95D979421}' = 'DirectPlay8Address'
}
$cfgKey = 'HKCU:\Software\TW1MP'

foreach ($id in $clsids.Keys) {
    $key = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id\InprocServer32"
    if ($Status) {
        if (Test-Path $key) { "$($clsids[$id]): " + (Get-ItemProperty $key).'(default)' }
        else { "$($clsids[$id]): (Windows-Standard, keine Umleitung)" }
        continue
    }
    if ($Remove) {
        $parent = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id"
        if (Test-Path $parent) { Remove-Item $parent -Recurse -Force; "entfernt: $($clsids[$id])" }
    } else {
        if (-not (Test-Path $dll)) { throw "dpnetrelay.dll fehlt neben diesem Skript: $dll" }
        Unblock-File $dll -ErrorAction SilentlyContinue
        if (-not (Test-Path $key)) { New-Item $key -Force | Out-Null }
        Set-ItemProperty $key -Name '(default)' -Value $dll
        Set-ItemProperty $key -Name 'ThreadingModel' -Value 'Both'
        "aktiv: $($clsids[$id]) -> $dll"
    }
}
if ($Status) {
    $r = (Get-ItemProperty $cfgKey -ErrorAction SilentlyContinue).Relay
    if ($r) { "Relay (HKCU\Software\TW1MP\Relay): $r" } else { "Relay: nicht gesetzt (DLL nimmt TW1MP_RELAY oder den eingebauten Standard)" }
    return
}
if ($Relay) {
    if (-not (Test-Path $cfgKey)) { New-Item $cfgKey -Force | Out-Null }
    Set-ItemProperty $cfgKey -Name 'Relay' -Value $Relay
    "Relay-Adresse gesetzt: $Relay"
}
if ($Remove) {
    # A leftover test address would outrank natresolver and the built-in
    # server the next time the DLL loads (e.g. through TWFix).
    if ((Get-ItemProperty $cfgKey -ErrorAction SilentlyContinue).Relay) {
        Remove-ItemProperty $cfgKey -Name 'Relay' -ErrorAction SilentlyContinue
        "Relay-Adresse entfernt"
    }
    "Relay-Replacement entfernt - Spiel nutzt wieder die Windows-dpnet.dll."
}
else { "Relay-Replacement aktiv. Log: " + (Join-Path $PSScriptRoot 'dpnetrelay.log') }
