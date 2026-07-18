# Handoff: Surface als 2. Two-Worlds-1-Client einrichten (LAN-Multiplayer-Test)

Dieser Text ist für einen **frischen Claude-Chat auf dem Surface Pro 7**.
Bitte Schritt für Schritt abarbeiten und am Ende (bzw. bei jedem Blocker)
zurückmelden. Kommunikation Deutsch, alles Technische verifizieren statt raten.

## Worum es geht

Wir beleben den Multiplayer von **Two Worlds 1** (2007) mit einem selbst
geschriebenen Community-Lobby-Server wieder. Auf dem **Haupt-PC** von Marco
läuft dieser Lobby-Server und dort wird auch das Multiplayer-Spiel gehostet.
Der **Surface** soll der **zweite Spieler** werden und dem gehosteten Spiel
im LAN beitreten. Ziel dieses Tests: beweisen, dass zwei echte Clients über
den Lobby-Server zusammenfinden und eine DirectPlay-Spielsitzung aufbauen.

**Wichtig:** Dieser Test nutzt das **normale Windows-DirectPlay** (nicht die
experimentelle Replacement-DLL). Auf dem Surface muss also nur das
Windows-DirectPlay funktionieren.

## Feste Fakten (vom Haupt-PC)

- **Lobby-Server-IP (Haupt-PC): `192.168.1.136`**, Port **17171**.
- Der Surface **muss im selben WLAN/Subnetz** sein, also eine IP `192.168.1.x`
  haben. Bitte als Erstes prüfen: `ipconfig` → ist die IPv4-Adresse `192.168.1.*`?
  Wenn nicht (z. B. `192.168.0.*` oder anderes Netz), sind die Geräte nicht im
  selben LAN → stoppen und Marco sagen (beide ins gleiche WLAN).
- Der Server bindet auf alle Interfaces und ist aus dem LAN erreichbar, sobald
  Marco auf dem Haupt-PC die Firewall-Freigabe (Port 17171) gemacht und den
  Server gestartet hat. Das ist Marcos Aufgabe auf dem Haupt-PC, nicht deine.

## Deine Aufgaben auf dem Surface

### 1. Vorprüfung
- `ipconfig` → IPv4 muss `192.168.1.*` sein (siehe oben).
- Ist Two Worlds installiert? Pfad steht in der Registry:
  `HKLM\SOFTWARE\WOW6432Node\Reality Pump\TwoWorlds\FileSystem` → Wert `DataPath`.
  Die Spiel-EXE ist dort `TwoWorlds_RADEON.exe` (oder `TwoWorlds.exe`).
- Erreicht der Surface den Server? Teste:
  `Test-NetConnection -ComputerName 192.168.1.136 -Port 17171`
  → `TcpTestSucceeded : True` erwartet. Wenn False: Marco muss auf dem Haupt-PC
  die Firewall-BAT ausführen und den Server starten; dann erneut testen.

### 2. DirectPlay aktivieren (KRITISCH)
Two Worlds stürzt beim Spielstart in `dpnet.dll` ab, wenn die Windows-
Komponente **DirectPlay** nicht aktiv ist. In einer **Admin-PowerShell**:
```powershell
Get-WindowsOptionalFeature -Online -FeatureName DirectPlay   # Status pruefen
# Falls "Disabled":
Enable-WindowsOptionalFeature -Online -FeatureName DirectPlay -All
# danach Neustart. Falls schon "Enabled", nichts tun.
```
Ohne Admin kannst du das nicht setzen → dann Marco bitten, es am Surface als
Admin auszuführen (er muss im UAC-Dialog "Ja" klicken).

### 3. Registry: Lobby-Server eintragen (kein Admin nötig, HKCU)
Das Spiel liest seine Serverliste aus HKCU. **Erst sichern**, dann den
Haupt-PC vorn an die Liste hängen:
```powershell
# Backup
reg export "HKCU\SOFTWARE\Reality Pump\TwoWorlds\Network" "$env:USERPROFILE\Desktop\tw-network-backup.reg" /y
# Server vorn anhaengen
$k = 'HKCU:\SOFTWARE\Reality Pump\TwoWorlds\Network'
$old = (Get-ItemProperty $k -ErrorAction SilentlyContinue).EarthNet_ServersAddresses
if (-not $old) { $old = '' }
if ($old -notmatch 'Marco LAN') {
  Set-ItemProperty $k -Name 'EarthNet_ServersAddresses' -Value ('"Marco LAN""192.168.1.136"' + $old)
}
(Get-ItemProperty $k).EarthNet_ServersAddresses   # Kontrolle
```
`EarthNet_ServerPort` sollte 17171 sein (Standard). Falls der Network-Key gar
nicht existiert, hat das Spiel dort noch nie geschrieben — dann Marco sagen,
dass er auf dem Surface einmal ins Multiplayer-Menü geht (das legt den Key an),
danach diesen Schritt wiederholen.

### 4. Anderer Benutzername (WICHTIG wegen evtl. gleichem Key)
Beide Geräte haben evtl. dieselbe Seriennummer. Der Server ist bereits so
gestellt, dass das egal ist (Serial-Bindung aus). ABER beide Clients dürfen
**nicht denselben Benutzernamen** verwenden — der Haupt-PC ist `marco19942`.
Der Surface muss einen **anderen** Namen nutzen (z. B. `surface`).

Das Spiel loggt sich automatisch mit den gespeicherten Zugangsdaten ein. Damit
der Login-Dialog erscheint und du einen neuen Namen vergeben kannst, die
gespeicherten Zugangsdaten wegsichern:
```powershell
$prof = "$env:USERPROFILE\Saved Games\Two Worlds Saves\Players\default"
if (Test-Path "$prof\UserInfo.usr") { Rename-Item "$prof\UserInfo.usr" "UserInfo.usr.bak" }
```
(Reversibel: später zurückbenennen. Falls das Profil anders heißt als `default`,
den passenden Ordner unter `Players\` nehmen.)
Beim Verbinden erscheint dann der Login/Registrier-Dialog → dort Benutzername
**`surface`** (o. Ä.) mit beliebigem Passwort registrieren. Der Server legt den
Account automatisch an.

### 4b. NAT-Resolver abschalten (für LAN-Direktverbindung — WICHTIG)
Für ein LAN-Spiel muss die DirectPlay-P2P-Verbindung **direkt** über die
LAN-IP laufen, nicht über den externen NAT-Resolver (`warnet.2-worlds.com`).
Sonst schlägt der Beitritt mit „Verbindung fehlgeschlagen" fehl, obwohl man
sich in der Lobby sieht. In einer normalen PowerShell (kein Admin nötig):
```powershell
$k = 'HKCU:\SOFTWARE\Reality Pump\TwoWorlds\Network'
Set-ItemProperty $k -Name EarthNet_UseNATResolver        -Value 0 -Type DWord
Set-ItemProperty $k -Name EarthNet_AddNATResolverInHost  -Value 0 -Type DWord
Set-ItemProperty $k -Name EarthNet_AddNATResolverInClient -Value 0 -Type DWord
Get-ItemProperty $k | Select-Object EarthNet_UseNATResolver,EarthNet_AddNATResolverInHost,EarthNet_AddNATResolverInClient
```
(Falls der Network-Key noch nicht existiert: erst einmal ins Multiplayer-Menü
gehen, dann diesen Schritt.) **Nach der Änderung Two Worlds neu starten** —
die Netzwerkeinstellungen werden nur beim Spielstart gelesen.

### 5. Firewall fürs Spiel
Two Worlds hat meist schon eingehende Firewall-Regeln von Steam. Prüfen:
```powershell
Get-NetFirewallRule -DisplayName "*Two Worlds*" -ErrorAction SilentlyContinue |
  Select-Object DisplayName, Enabled, Direction
```
Für diesen Test **hostet der Haupt-PC**, der Surface verbindet sich nur nach
außen — eingehende Regeln auf dem Surface sind daher unkritisch. Nur falls
später der Surface selbst hosten soll, braucht er eingehende Freigaben für
`TwoWorlds_RADEON.exe`.

## Testablauf (macht Marco gemeinsam an beiden Geräten)

1. Haupt-PC: Lobby-Server läuft, Firewall frei, Marco loggt sich als
   `marco19942` ein, geht in eine Stadt und **erstellt ein Spiel** (F12 →
   Mission wählen → „Einstieg neuer Spieler möglich" anhaken → Erstellen).
   Noch **nicht** starten, bis der Surface beigetreten ist.
2. Surface: Spiel starten → **Netzwerk** → Server **„Marco LAN"** wählen →
   verbinden → als **`surface`** einloggen/registrieren → dieselbe **Stadt**
   betreten wie der Haupt-PC.
3. Surface sollte das von Marco erstellte **Spiel in der Liste sehen** →
   beitreten (Teilnehmen).
4. Haupt-PC startet das Spiel (F12) → **beide** sollten in dieselbe Map laden
   und sich gegenseitig sehen.

## Was du zurückmelden sollst

Für jeden Schritt kurz: hat's geklappt oder woran hakt es. Besonders wichtig:
- `ipconfig`-Subnetz und `Test-NetConnection`-Ergebnis (kommt der Surface an den Server?)
- DirectPlay-Status (Enabled/Disabled, ob Neustart nötig war)
- Ob der Login/Registrier-Dialog mit neuem Namen kam
- Ob der Surface das gehostete Spiel in der Liste sieht und beitreten kann
- Ob die DirectPlay-Session zustande kommt oder wo/womit es abstürzt (bei
  einem Absturz: Windows-Ereignisanzeige / `Get-WinEvent` nach `TwoWorlds` +
  `dpnet.dll` schauen — das ist die typische Absturzsignatur)

Das Ergebnis gibt Marco dann zurück in den Haupt-PC-Chat, wo der Server und der
restliche Code liegen.
