# Activate (or remove) the DirectPlay8 replacement for the current user.
#
# The 32-bit game resolves the DirectPlay8 CLSIDs through the WOW6432Node
# view of the class registry; writing them under HKCU needs no admin and
# overrides the system dpnet.dll for this user only. Removing restores the
# stock Windows DirectPlay.
#
#   .\register.ps1            -> use the replacement
#   .\register.ps1 -Remove    -> restore stock DirectPlay
param([switch]$Remove)

$dll = Join-Path $PSScriptRoot 'dpnetreplace.dll'
# Only the objects the game creates need redirecting: Peer + Address.
$clsids = [ordered]@{
    '{286F484D-375E-4458-A272-B138E2F80A6A}' = 'DirectPlay8Peer'
    '{934A9523-A3CA-4BC5-ADA0-D6D95D979421}' = 'DirectPlay8Address'
}

foreach ($id in $clsids.Keys) {
    $key = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id\InprocServer32"
    if ($Remove) {
        $parent = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id"
        if (Test-Path $parent) { Remove-Item $parent -Recurse -Force; "entfernt: $($clsids[$id])" }
    } else {
        if (-not (Test-Path $key)) { New-Item $key -Force | Out-Null }
        Set-ItemProperty $key -Name '(default)' -Value $dll
        Set-ItemProperty $key -Name 'ThreadingModel' -Value 'Both'
        "aktiv: $($clsids[$id]) -> $dll"
    }
}
if ($Remove) { "Replacement entfernt - Spiel nutzt wieder die Windows-dpnet.dll." }
else { "Replacement aktiv. Log: C:\Users\marco\Desktop\twMP\dpnetreplace.log" }
