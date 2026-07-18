# Register (or remove) the DirectPlay8 shim for the current user only.
# HKCU\Software\Classes takes precedence over HKLM for this user, so no
# administrator rights are needed and nothing system-wide is touched.
#
#   .\register.ps1            -> activate the shim
#   .\register.ps1 -Remove    -> restore the stock behaviour
param([switch]$Remove)

$dll = Join-Path $PSScriptRoot 'dpnetshim.dll'
$clsids = [ordered]@{
    '{286F484D-375E-4458-A272-B138E2F80A6A}' = 'DirectPlay8Peer'
    '{743F1DC6-5ABA-429F-8BDF-C54D03253DC2}' = 'DirectPlay8Client'
    '{DA825E1B-6830-43D7-835D-0B5AD82956A2}' = 'DirectPlay8Server'
    '{934A9523-A3CA-4BC5-ADA0-D6D95D979421}' = 'DirectPlay8Address'
}

foreach ($id in $clsids.Keys) {
    $key = "HKCU:\Software\Classes\CLSID\$id\InprocServer32"
    if ($Remove) {
        $parent = "HKCU:\Software\Classes\CLSID\$id"
        if (Test-Path $parent) {
            Remove-Item $parent -Recurse -Force
            "entfernt: $($clsids[$id])"
        }
    } else {
        if (-not (Test-Path $key)) { New-Item $key -Force | Out-Null }
        Set-ItemProperty $key -Name '(default)' -Value $dll
        Set-ItemProperty $key -Name 'ThreadingModel' -Value 'Both'
        "registriert: $($clsids[$id]) -> $dll"
    }
}
if ($Remove) { "Shim entfernt - Spiel nutzt wieder die Windows-dpnet.dll." }
else { "Shim aktiv. Log: C:\Users\marco\Desktop\twMP\dpnetshim.log" }
