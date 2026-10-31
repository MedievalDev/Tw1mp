# Register (or remove) the DirectPlay8 capture shim for the current user.
#
# HKCU\Software\Classes takes precedence over HKLM for this user, so no
# administrator rights are needed and nothing system-wide is touched. The
# 32-bit game reads the WOW6432Node view of the class registry, so that is
# where the redirect must be written.
#
# The shim is transparent: the real Windows dpnet.dll still does all the
# work, the shim only logs. Log: dpnetcapture.log next to the DLL.
#
#   .\register.ps1            -> activate the capture shim
#   .\register.ps1 -Remove    -> restore the stock behaviour
#   .\register.ps1 -Status    -> show what the CLSIDs currently point at
param([switch]$Remove, [switch]$Status)

$dll = Join-Path $PSScriptRoot 'dpnetcapture.dll'
$clsids = [ordered]@{
    '{286F484D-375E-4458-A272-B138E2F80A6A}' = 'DirectPlay8Peer'
    '{934A9523-A3CA-4BC5-ADA0-D6D95D979421}' = 'DirectPlay8Address'
}

foreach ($id in $clsids.Keys) {
    $key = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id\InprocServer32"
    if ($Status) {
        if (Test-Path $key) { "$($clsids[$id]): " + (Get-ItemProperty $key).'(default)' }
        else { "$($clsids[$id]): (Windows-Standard, keine Umleitung)" }
        continue
    }
    if ($Remove) {
        $parent = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id"
        if (Test-Path $parent) {
            Remove-Item $parent -Recurse -Force
            "entfernt: $($clsids[$id])"
        }
    } else {
        if (-not (Test-Path $dll)) { throw "dpnetcapture.dll fehlt neben diesem Skript: $dll" }
        Unblock-File $dll -ErrorAction SilentlyContinue
        if (-not (Test-Path $key)) { New-Item $key -Force | Out-Null }
        Set-ItemProperty $key -Name '(default)' -Value $dll
        Set-ItemProperty $key -Name 'ThreadingModel' -Value 'Both'
        "registriert: $($clsids[$id]) -> $dll"
    }
}
if ($Status) { return }
if ($Remove) { "Capture-Shim entfernt - Spiel nutzt wieder die Windows-dpnet.dll direkt." }
else { "Capture-Shim aktiv. Log: " + (Join-Path $PSScriptRoot 'dpnetcapture.log') }
