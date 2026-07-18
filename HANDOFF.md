# Status — Test mit dem echten Spiel (aktualisiert)

Stand: 2026-07-18 · Branch `claude/two-worlds-multiplayer-server-otqmlr` · [PR #1](https://github.com/MedievalDev/Tw1mp/pull/1)

Der ursprüngliche Handoff-Plan („lokaler Test mit dem echten Spiel") ist
durchgeführt. Dieses Dokument hält die Ergebnisse und die offenen Punkte fest.

## Mit dem echten Client (Two Worlds 1.7, Steam Epic Edition) verifiziert

- Verbindung ohne `hosts`-Umleitung: Das Spiel liest seine Serverliste aus
  `HKCU\SOFTWARE\Reality Pump\TwoWorlds\Network\EarthNet_ServersAddresses`
  (Format `"Name""Host"`-Paare, Port aus `EarthNet_ServerPort`, Standard
  17171). Eintrag ergänzen → Server erscheint im Auswahldialog. Kein Admin
  nötig; Details im README.
- Auto-Login mit gespeicherten Zugangsdaten, Auto-Registrierung, MOTD,
  Städteliste, Stadt betreten/verlassen (mehrfach), Chat, Spiel
  erstellen/starten/beenden (Lobby-Seite), `/getguildrankpoints`,
  Spielstands-Up/Download (`/getplayerdata`/`/setplayerdata`).
- Charakter-Download vom noch laufenden offiziellen Server
  (`warnet.2-worlds.com` = `netserver.2-worlds.com`, eine Azure-IP;
  `hawk.2-worlds-us.com` ist tot) und Import in den lokalen Server.
- Item-Editor: Mengenfeld im Charakterformat an echten Stacks verifiziert
  (drei Lockpick-Stacks 54/7/621, Steinpilz-Test 7→77 im Spiel sichtbar).
  Das Spiel prüft keine Checksumme über den Charakter-Blob.
- Unbekannte Client-Kommandos entdeckt (weder Doku noch Referenzserver
  kennen sie): `/ladder` und `/guildsladder "1"` — werden stumm ignoriert,
  Client läuft normal weiter. Kandidaten für späteres Nachrüsten.

## Wichtigster Befund: DirectPlay-Crash ist client-/OS-seitig

Das Starten einer Spielsitzung crasht den Client in `dpnet.dll`
(DirectPlay8) — **byte-identisch gegen unseren Server und gegen den
offiziellen** (Windows-Fehlerbucket in beiden Fällen gleich). Es ist kein
Serverfehler. Fix laut Recherche (PCGamingWiki/Steam/GOG-Konsens, auf
diesem Rechner noch nicht durchgeführt):

```powershell
# Admin-PowerShell:
Get-WindowsOptionalFeature -Online -FeatureName DirectPlay   # Status
Enable-WindowsOptionalFeature -Online -FeatureName DirectPlay -All
# falls schon Enabled, 32-bit-Komponente auffrischen:
DISM /Online /Cleanup-Image /RestoreHealth
sfc /scannow
```

Danach Neustart und Spielstart erneut testen. Niemals `dpnet.dll` von
DLL-Seiten laden — Windows-Systemkomponente.

Die Spielsitzung selbst ist P2P-DirectPlay; der Host bewirbt seine
**private** LAN-IP (im Mitschnitt gesehen). Internet-Spiele brauchen daher
VPN (ZeroTier/Tailscale/Hamachi) oder Portfreigaben beim Host; der
NAT-Resolver der offiziellen Server (`EarthNet_UseNATResolver`) wird von
uns nicht angeboten.

## Serverseitige Korrekturen aus dem Test

- Spiel-Lifecycle-Nachrichten (`$game`/`&game`/`/&chatchanneluser`/…)
  gehen nur noch an *andere* Spieler im Kanal, nie an die Spieler des
  betroffenen Spiels; kein doppeltes `&game` mehr nach `/&game`. Das
  entspricht buglords feld-getestetem Solo-Server (TW1CS sendete mehr,
  wurde mit diesem Ablauf aber nie gegen den echten Client getestet).
- `/updheropos` echot die eigene Position nicht mehr an den Bewegenden.
- Wire-Logging: `-l DEBUG` loggt jetzt RX/TX jedes Kommandos.

## Seit dem Test hinzugekommen

- **Desktop-UI** (`TW1MP-UI.pyw`, Windows, tkinter + Win32-Tray via
  ctypes — weiterhin keine Abhängigkeiten): Start/Stop, Live-Log,
  Spielerliste, Tray-Betrieb, Charakterverwaltung.
- **Charakter-Transfer** (`tw1mp/savegame.py`): Download vom
  offiziellen/beliebigen Lobby-Server (Serial + Login kommen aus der
  lokalen Spielinstallation), Import mit Backup des ersetzten Charakters.
- **Item-Editor** (`tw1mp/chardata.py` + UI-Dialog): Mengen vorhandener
  Stacks ändern. Ergebnis liegt als `PlayerData/<id>.modded.bin` neben dem
  Original; der Server liefert die Variante **nur an einen allein
  eingeloggten Spieler** und Session-Saves gehen in die Variante zurück —
  das Original kann von einer Modded-Session nie überschrieben werden.
- Testsuite auf 60 Tests erweitert (Serial-Algorithmus byte-identisch zu
  buglords Referenz verifiziert; Solo/Multiplayer-Serving End-to-End).

## Offene Punkte

- [ ] DirectPlay-Fix durchführen, dann: Spielstart, 2-Spieler-Test im LAN
      (zweite Maschine oder VM), DirectPlay-Session, Spielende, Spielstand
- [ ] `/ladder` & `/guildsladder` beantworten (Format unbekannt — ggf. am
      offiziellen Server mitschneiden, solange er noch läuft)
- [ ] PR aus dem Draft nehmen, sobald der 2-Spieler-Test durch ist
- [ ] Betrieb: systemd/Docker (Raspberry Pi reicht locker), Backups von
      `ServerData.db` + `PlayerData/`
- Upstream-Fund: buglords `TW1PDBackup.pyw` lehnt Registry-Serials in
  Kleinschreibung ab (`.upper()` fehlt im Registry-Pfad) — Bugreport wert.
