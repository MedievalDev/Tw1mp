# =====================================================================
#  TW1MP - one-click setup for Two Worlds 1 (2007) multiplayer
#
#  Two steps, no admin, no manual input:
#    1) adds the community server to the game's server list
#       (per-user registry HKCU, makes a .reg backup first)
#    2) installs + activates the TW1MP DirectPlay8 replacement DLL
#       (per-user COM registration under WOW6432Node - no admin), so the
#       game's multiplayer sessions no longer depend on the flaky Windows
#       DirectPlay component.
#
#  Meant to be fetched from GitHub and run:
#    powershell -ExecutionPolicy Bypass -Command "iex (irm https://raw.githubusercontent.com/MedievalDev/Tw1mp/main/client-setup/TW1MP-Setup.ps1)"
#  or just double-click TW1MP-Setup.bat.
#
#  Env vars:  TW1MP_SKIP_DIRECTPLAY=1  -> only the server-list step
#             TW1MP_REMOVE=1           -> undo the DirectPlay replacement
# =====================================================================

# ---- the server this installer points at (edit to repoint) ----------
$Name       = 'TW1MP Community Server'
$ServerHost = '87.106.168.34'
$Port       = 17171
$Repo       = 'MedievalDev/Tw1mp'
$Ref        = 'main'
# ---------------------------------------------------------------------

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$KeyPath   = 'HKCU:\SOFTWARE\Reality Pump\TwoWorlds\Network'
$ExportKey = 'HKCU\SOFTWARE\Reality Pump\TwoWorlds\Network'
$ValueName = 'EarthNet_ServersAddresses'
$PortName  = 'EarthNet_ServerPort'
$SkipDP    = ($env:TW1MP_SKIP_DIRECTPLAY -eq '1')
$Remove    = ($env:TW1MP_REMOVE -eq '1')

# DirectPlay8 CLSIDs the game creates (Peer + Address); redirected per-user.
$Clsids = [ordered]@{
    '{286F484D-375E-4458-A272-B138E2F80A6A}' = 'DirectPlay8Peer'
    '{934A9523-A3CA-4BC5-ADA0-D6D95D979421}' = 'DirectPlay8Address'
}
$DllDir = Join-Path $env:LOCALAPPDATA 'TW1MP'
$Dll    = Join-Path $DllDir 'dpnetreplace.dll'
$DllUrl = "https://raw.githubusercontent.com/$Repo/$Ref/directplay-replace/dpnetreplace.dll"

Write-Host ""
Write-Host "  ===================================================" -ForegroundColor Yellow
Write-Host "   TW1MP  -  Two Worlds 1 community server setup"      -ForegroundColor Yellow
Write-Host "   Server: $Name"                                      -ForegroundColor Yellow
Write-Host "           $ServerHost : $Port"                        -ForegroundColor Yellow
Write-Host "  ===================================================" -ForegroundColor Yellow

# ---------- undo path ------------------------------------------------
if ($Remove) {
    Write-Host ""
    Write-Host "  Entferne DirectPlay-Replacement (stock Windows-dpnet wird wieder genutzt)..." -ForegroundColor Cyan
    foreach ($id in $Clsids.Keys) {
        $parent = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id"
        if (Test-Path $parent) { Remove-Item $parent -Recurse -Force; Write-Host "  - entfernt: $($Clsids[$id])" }
    }
    Write-Host "  Fertig. Server-Listeneintrag bleibt bestehen." -ForegroundColor Green
    return
}

# ---------- 1) registry: put the server in the game's list -----------
Write-Host ""
Write-Host "  [1/2] Server in die Spiel-Serverliste eintragen" -ForegroundColor Cyan
try {
    if ($Name -match '"' -or $ServerHost -match '"') { throw 'Name/Adresse duerfen keine (") enthalten.' }

    $existedBefore = Test-Path $KeyPath
    if (-not $existedBefore) {
        New-Item -Path $KeyPath -Force | Out-Null
        Write-Host "  - Registry-Key neu angelegt (Spiel hatte noch keine Netzwerk-Einstellungen)."
    } else {
        try {
            $backup = Join-Path ([Environment]::GetFolderPath('Desktop')) ("TwoWorlds-Network-Backup-{0:yyyyMMdd-HHmmss}.reg" -f (Get-Date))
            reg.exe export $ExportKey $backup /y | Out-Null
            Write-Host "  - Backup der aktuellen Serverliste: $backup"
        } catch { Write-Host "  - Backup fehlgeschlagen (weiter): $_" -ForegroundColor Yellow }
    }

    $current = (Get-ItemProperty -Path $KeyPath -Name $ValueName -ErrorAction SilentlyContinue).$ValueName
    if ($null -eq $current) { $current = "" }

    # rebuild: drop any existing pair for this host, prepend a fresh one on top
    $pairs = [regex]::Matches($current, '"([^"]*)""([^"]*)"')
    $kept  = foreach ($p in $pairs) {
        if ($p.Groups[2].Value -ne $ServerHost) { '"' + $p.Groups[1].Value + '""' + $p.Groups[2].Value + '"' }
    }
    $entry = '"' + $Name + '""' + $ServerHost + '"'
    Set-ItemProperty -Path $KeyPath -Name $ValueName -Value ($entry + ($kept -join ''))
    Write-Host "  - '$Name' steht jetzt ganz oben in der Serverliste." -ForegroundColor Green

    $port = (Get-ItemProperty -Path $KeyPath -Name $PortName -ErrorAction SilentlyContinue).$PortName
    if ($null -eq $port) {
        New-ItemProperty -Path $KeyPath -Name $PortName -Value $Port -PropertyType DWord -Force | Out-Null
        Write-Host "  - $PortName = $Port gesetzt."
    } elseif ($port -ne $Port) {
        Write-Host "  - Hinweis: $PortName ist $port, Server nutzt $Port. Bei Problemen in regedit anpassen." -ForegroundColor Yellow
    } else { Write-Host "  - $PortName = $Port (passt)." }
} catch {
    Write-Host "  ! Registry-Schritt fehlgeschlagen: $_" -ForegroundColor Red
}

# ---------- 2) DirectPlay replacement (per-user, no admin) -----------
Write-Host ""
Write-Host "  [2/2] DirectPlay-Replacement installieren (per-user, ohne Admin)" -ForegroundColor Cyan
if ($SkipDP) {
    Write-Host "  - Uebersprungen (TW1MP_SKIP_DIRECTPLAY=1)." -ForegroundColor DarkGray
} else {
    try {
        New-Item -ItemType Directory -Force -Path $DllDir | Out-Null
        Write-Host "  - Lade Replacement-DLL von GitHub..."
        Invoke-WebRequest -Uri $DllUrl -OutFile $Dll -UseBasicParsing
        $len = (Get-Item $Dll).Length
        if ($len -lt 4096) { throw "DLL-Download zu klein ($len Bytes) - Abbruch." }
        Write-Host "  - DLL gespeichert: $Dll ($len Bytes)"

        foreach ($id in $Clsids.Keys) {
            $key = "HKCU:\Software\Classes\WOW6432Node\CLSID\$id\InprocServer32"
            if (-not (Test-Path $key)) { New-Item $key -Force | Out-Null }
            Set-ItemProperty $key -Name '(default)'      -Value $Dll
            Set-ItemProperty $key -Name 'ThreadingModel' -Value 'Both'
            Write-Host "  - registriert: $($Clsids[$id])" -ForegroundColor Green
        }
        Write-Host "  - Replacement aktiv - stock Windows-dpnet wird fuer dich umgangen." -ForegroundColor Green
    } catch {
        Write-Host "  ! Replacement-Schritt fehlgeschlagen: $_" -ForegroundColor Red
        Write-Host "    Die Lobby (Server sehen, Login, Chat, Charaktere) funktioniert trotzdem." -ForegroundColor Yellow
    }
}

# ---------- done -----------------------------------------------------
Write-Host ""
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host "   Fertig. Starte Two Worlds - '$Name'"                -ForegroundColor Green
Write-Host "   erscheint in der Server-Auswahl."                   -ForegroundColor Green
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Internet-Coop: im LAN laeuft's direkt. Uebers Internet brauchen die"  -ForegroundColor DarkGray
Write-Host "  Mitspieler aktuell noch ein VPN (ZeroTier/Tailscale/Hamachi) - ein"    -ForegroundColor DarkGray
Write-Host "  eigener NAT-Resolver fuer VPN-freies Spiel ist noch in Arbeit."        -ForegroundColor DarkGray
Write-Host "  Rueckgaengig machen: dieses Setup mit  TW1MP_REMOVE=1  erneut starten." -ForegroundColor DarkGray
Write-Host ""
