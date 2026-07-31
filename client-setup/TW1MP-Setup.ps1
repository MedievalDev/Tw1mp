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
    Get-NetFirewallRule -DisplayName 'TW1MP - *' -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    Write-Host "  - Firewall-Freigaben entfernt (falls als Admin gesetzt)."
    Write-Host "  Fertig. Server-Listeneintrag bleibt bestehen." -ForegroundColor Green
    return
}

# ---------- 1) registry: put the server in the game's list -----------
Write-Host ""
Write-Host "  [1/5] Server in die Spiel-Serverliste eintragen" -ForegroundColor Cyan
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
Write-Host "  [2/5] DirectPlay-Replacement installieren (per-user, ohne Admin)" -ForegroundColor Cyan
if ($SkipDP) {
    Write-Host "  - Uebersprungen (TW1MP_SKIP_DIRECTPLAY=1)." -ForegroundColor DarkGray
} else {
    try {
        New-Item -ItemType Directory -Force -Path $DllDir | Out-Null
        Write-Host "  - Lade Replacement-DLL von GitHub..."
        Invoke-WebRequest -Uri $DllUrl -OutFile $Dll -UseBasicParsing
        $len = (Get-Item $Dll).Length
        if ($len -lt 4096) { throw "DLL-Download zu klein ($len Bytes) - Abbruch." }
        # strip Mark-of-the-Web so Defender/SmartScreen doesn't block the load
        try { Unblock-File -Path $Dll -ErrorAction SilentlyContinue } catch {}
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

# ---------- 3) LAN: NAT-Resolver aus ---------------------------------
# Steht der Resolver an, laesst sich der Client seine Adresse von
# warnet.2-worlds.com sagen und kuendigt im LAN eine Adresse an, die es hier
# gar nicht gibt (real gesehen: 192.168.0.58 auf einem Rechner, der nur
# 192.168.1.136 hat). Der Beitritt laeuft dann ins Leere.
Write-Host ""
Write-Host "  [3/5] NAT-Resolver abschalten (fuer LAN-Spiele)" -ForegroundColor Cyan
try {
    foreach ($n in 'EarthNet_UseNATResolver','EarthNet_AddNATResolverInHost','EarthNet_AddNATResolverInClient') {
        Set-ItemProperty -Path $KeyPath -Name $n -Value 0 -Type DWord
    }
    Write-Host "  - Aus. Der Client nutzt jetzt seine echte LAN-Adresse." -ForegroundColor Green
} catch {
    Write-Host "  ! Fehlgeschlagen: $_" -ForegroundColor Red
}

# ---------- 4) Firewall: eingehendes UDP fuer das Spiel ---------------
# Der Host bindet einen UDP-Port und wartet darauf, dass Mitspieler sich
# melden. Ohne Freigabe verwirft Windows diese Pakete stumm - der Beitritt
# scheitert mit "Verbindung fehlgeschlagen", obwohl in der Lobby alles passt.
Write-Host ""
Write-Host "  [4/5] Firewall: eingehende Spielverbindungen erlauben" -ForegroundColor Cyan
$gameDir = $null
foreach ($rk in 'HKLM:\SOFTWARE\WOW6432Node\Reality Pump\TwoWorlds\FileSystem',
                'HKLM:\SOFTWARE\Reality Pump\TwoWorlds\FileSystem') {
    $dp = (Get-ItemProperty $rk -ErrorAction SilentlyContinue).DataPath
    if ($dp -and (Test-Path $dp)) { $gameDir = $dp; break }
}
if (-not $gameDir) {
    Write-Host "  - Spielordner nicht gefunden, uebersprungen." -ForegroundColor Yellow
} else {
    $exes = @('TwoWorlds_RADEON.exe','TwoWorlds.exe','TwoWorldsExtended.exe','2WSG.exe') |
            ForEach-Object { Join-Path $gameDir $_ } | Where-Object { Test-Path $_ }
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    # Regeln anlegen; ohne Adminrechte einmalig ueber UAC nachfragen.
    $mk = {
        param($paths)
        foreach ($exe in $paths) {
            $nm = "TW1MP - " + (Split-Path $exe -Leaf)
            Get-NetFirewallRule -DisplayName $nm -ErrorAction SilentlyContinue |
                Remove-NetFirewallRule -ErrorAction SilentlyContinue
            New-NetFirewallRule -DisplayName $nm -Direction Inbound -Action Allow `
                -Protocol UDP -Program $exe -Profile Any | Out-Null
        }
    }
    try {
        if ($isAdmin) {
            & $mk $exes
            Write-Host "  - Freigaben gesetzt fuer: $(($exes | Split-Path -Leaf) -join ', ')" -ForegroundColor Green
        } else {
            Write-Host "  - Dafuer sind Adminrechte noetig. Es erscheint gleich eine"
            Write-Host "    Windows-Abfrage ('Moechten Sie zulassen...') - bitte mit Ja"
            Write-Host "    bestaetigen. Angelegt werden nur eingehende UDP-Freigaben"
            Write-Host "    fuer die Two-Worlds-Programme, sonst nichts."
            $inner = "`$e=@('" + ($exes -join "','") + "'); " + $mk.ToString().Replace('param($paths)','$paths=$e')
            $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
            $p = Start-Process powershell -Verb RunAs -Wait -PassThru `
                    -ArgumentList '-NoProfile','-WindowStyle','Hidden','-EncodedCommand',$enc
            if ($p.ExitCode -eq 0) {
                Write-Host "  - Freigaben gesetzt." -ForegroundColor Green
            } else {
                Write-Host "  - Abgebrochen. Ohne Freigabe koennen Mitspieler deiner Partie" -ForegroundColor Yellow
                Write-Host "    nicht beitreten. Spaeter nachholbar: dieses Setup als Admin starten." -ForegroundColor Yellow
            }
        }
    } catch {
        Write-Host "  ! Fehlgeschlagen: $_" -ForegroundColor Red
    }
}

# ---------- 5) Text-Eingabe-Fix --------------------------------------
# Bekannter Bug auf modernen Windows: In Textfeldern (Login, Chat, CD-Key)
# kommt kaum ein Tastendruck an. Das Spiel holt Tastaturmeldungen mit einem
# PeekMessageA-Aufruf, dessen Filter sie verwirft. Der Fix leitet genau
# diesen einen Aufruf auf einen kleinen Wrapper im Null-Padding am Ende der
# Code-Sektion um, der den Filter auf WM_KEYFIRST..WM_KEYLAST zwingt.
# Identisch zu tools/tw_textinput_patch.py, hier ohne Python-Abhaengigkeit.
Write-Host ""
Write-Host "  [5/5] Text-Eingabe-Fix (Tippen in Login/Chat/CD-Key)" -ForegroundColor Cyan

# Wrapper-Code; Bytes 26..29 sind die IAT-Adresse von PeekMessageA (je Build).
$wrapTpl = [byte[]](0x55,0x89,0xE5,0x57,0x8B,0x45,0x18,0x50,
                    0x68,0x09,0x01,0x00,0x00, 0x68,0x00,0x01,0x00,0x00,
                    0x6A,0x00, 0x8B,0x45,0x08,0x50,
                    0x8B,0x3D,0,0,0,0, 0xFF,0xD7, 0x5F,0x5D, 0xC2,0x14,0x00)
$builds = @{
  'FF0F7C0E6D180847006A1D25D125F8B4' = @{ Name='TwoWorlds.exe'; IatVa=0x009753A8
      CallOff=0x002DFB9A; CallOrig=[byte[]](0xA8,0x53); CallNew=[byte[]](0x18,0x49)
      SlotOff=0x00573D18; SlotVa=0x00974920; WrapOff=0x00573D20 }
  '948D259CF4C7E472E37CCBECD5358BED' = @{ Name='TwoWorlds_RADEON.exe'; IatVa=0x0096D398
      CallOff=0x002DFC5A; CallOrig=[byte[]](0x98,0xD3); CallNew=[byte[]](0x40,0xCA)
      SlotOff=0x0056BE40; SlotVa=0x0096CA48; WrapOff=0x0056BE48 }
}
if (-not $gameDir) {
    Write-Host "  - Spielordner nicht gefunden, uebersprungen." -ForegroundColor Yellow
} else {
    foreach ($exe in @('TwoWorlds.exe','TwoWorlds_RADEON.exe')) {
        $path = Join-Path $gameDir $exe
        if (-not (Test-Path $path)) { continue }
        try {
            $data = [IO.File]::ReadAllBytes($path)
            $md5  = ([BitConverter]::ToString(
                        [Security.Cryptography.MD5]::Create().ComputeHash($data))
                    ).Replace('-','')
            $spec = $builds[$md5]
            if (-not $spec) {
                # Schon gepatcht? Dann steht am Aufrufort bereits das neue Muster.
                $done = $false
                foreach ($s in $builds.Values) {
                    if ($data[$s.CallOff] -eq $s.CallNew[0] -and
                        $data[$s.CallOff+1] -eq $s.CallNew[1]) { $done = $true }
                }
                if ($done) { Write-Host "  - $exe : bereits gepatcht." -ForegroundColor Green }
                else { Write-Host "  - $exe : unbekannter Build, nicht angefasst." -ForegroundColor Yellow }
                continue
            }
            # Wrapper mit der IAT-Adresse dieses Builds fuellen
            $wrap = $wrapTpl.Clone()
            [Array]::Copy([BitConverter]::GetBytes([uint32]$spec.IatVa), 0, $wrap, 26, 4)
            $edits = @(
                @{ Off=$spec.CallOff; Expect=$spec.CallOrig; New=$spec.CallNew },
                @{ Off=$spec.SlotOff; Expect=[byte[]](0,0,0,0)
                   New=[BitConverter]::GetBytes([uint32]$spec.SlotVa) },
                @{ Off=$spec.WrapOff; Expect=[byte[]]::new($wrap.Length); New=$wrap }
            )
            # Erst alle Stellen pruefen, dann erst schreiben.
            $ok = $true
            foreach ($e in $edits) {
                for ($i=0; $i -lt $e.Expect.Length; $i++) {
                    if ($data[$e.Off+$i] -ne $e.Expect[$i]) { $ok = $false; break }
                }
                if (-not $ok) { break }
            }
            if (-not $ok) {
                Write-Host "  - $exe : unerwartete Bytes, nicht angefasst." -ForegroundColor Yellow
                continue
            }
            if (-not (Test-Path "$path.bak")) { Copy-Item $path "$path.bak" }
            foreach ($e in $edits) { [Array]::Copy($e.New, 0, $data, $e.Off, $e.New.Length) }
            [IO.File]::WriteAllBytes($path, $data)
            Write-Host "  - $exe : gepatcht (Backup: $exe.bak)" -ForegroundColor Green
        } catch {
            Write-Host "  - $exe : fehlgeschlagen ($_)" -ForegroundColor Yellow
        }
    }
}

# ---------- done -----------------------------------------------------
Write-Host ""
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host "   Fertig. Starte Two Worlds - '$Name'"                -ForegroundColor Green
Write-Host "   erscheint in der Server-Auswahl."                   -ForegroundColor Green
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Falls andere deinen Namen in der Lobby sehen, deine Spielfigur aber"  -ForegroundColor DarkGray
Write-Host "  unsichtbar bleibt (und ihre bei dir): Das Spiel meldet dann eine"      -ForegroundColor DarkGray
Write-Host "  ungueltige Kennung. Abhilfe: Produktaktivierung erneut durchlaufen"    -ForegroundColor DarkGray
Write-Host "  (Two Worlds Control Panel -> Aktivierung loeschen, Spiel starten,"     -ForegroundColor DarkGray
Write-Host "  Seriennummer neu eingeben). Danach stimmt die Kennung."                -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Internet-Coop: im LAN laeuft's direkt. Uebers Internet brauchen die"  -ForegroundColor DarkGray
Write-Host "  Mitspieler aktuell noch ein VPN (ZeroTier/Tailscale/Hamachi) - ein"    -ForegroundColor DarkGray
Write-Host "  eigener NAT-Resolver fuer VPN-freies Spiel ist noch in Arbeit."        -ForegroundColor DarkGray
Write-Host "  Rueckgaengig machen: dieses Setup mit  TW1MP_REMOVE=1  erneut starten." -ForegroundColor DarkGray
Write-Host ""
