# Zwei-Spieler-Mitschnitt mit dem Capture-Shim

Ziel: eine echte Two-Worlds-Spielsitzung mit **zwei Clients über das
Windows-DirectPlay** aufzeichnen. Der Shim ist transparent, die Windows-
`dpnet.dll` macht weiter die Arbeit, der Shim schreibt nur mit, was das
Spiel von DirectPlay verlangt und zurückbekommt. Das Ergebnis ist die
Vorlage, gegen die die Relay-DLL gebaut wird. Auch eine Sitzung, die nach
einer Minute abstürzt, liefert alles Wichtige: Beitritt, Spielerliste,
Nachrichtenverkehr.

Das Log heißt `dpnetcapture.log` und liegt **neben der DLL**. Es wird nach
jeder Zeile geschrieben, ein Absturz verliert nichts. Es kann während des
Spiels gelesen werden.

## Was auf beide Rechner muss

Aus `directplay-capture/`:

- `dpnetcapture.dll`
- `register.ps1`

Beides in einen beliebigen Ordner legen, z. B. `%LOCALAPPDATA%\TW1MP-Capture`.
Für das Surface: der Ordner `FoxShare\TW1MP-Capture` wird per Syncthing
übertragen, dort liegen DLL und Skript bereit.

## Einrichten (pro Rechner, kein Admin)

1. Shim aktivieren:

```powershell
Set-Location "$env:LOCALAPPDATA\TW1MP-Capture"   # bzw. FoxShare\TW1MP-Capture
.\register.ps1
.\register.ps1 -Status     # muss auf dpnetcapture.dll zeigen
```

   Das trägt die DirectPlay8-CLSIDs unter `HKCU\Software\Classes\WOW6432Node`
   ein. Gilt für alle 32-Bit-Prozesse dieses Benutzers, also für jede
   Spiel-EXE (Steam, GOG, RADEON, Extended).

2. NAT-Resolver aus, sonst versucht das Windows-DirectPlay den Umweg über
   den offiziellen Resolver statt der direkten LAN-Verbindung (das war am
   19.7. die Voraussetzung für den gelungenen LAN-Beitritt):

```powershell
$k = 'HKCU:\SOFTWARE\Reality Pump\TwoWorlds\Network'
foreach ($n in 'EarthNet_UseNATResolver','EarthNet_AddNATResolverInHost','EarthNet_AddNATResolverInClient') {
  Set-ItemProperty $k -Name $n -Value 0 -Type DWord
}
```

   Rückgängig: dieselben drei Werte wieder auf 1 (Haupt-PC hatte
   UseNATResolver=1, AddNATResolverInHost=1, AddNATResolverInClient=0).

3. Windows-DirectPlay muss aktiviert sein (war es auf beiden Rechnern schon):

```powershell
Get-WindowsOptionalFeature -Online -FeatureName DirectPlay
```

4. Beide Rechner auf **denselben Lobby-Server**, gleiche Stadt. Nicht
   derselbe Benutzername.

## Rollen

- **Haupt-PC hostet.** Dort sind die eingehenden UDP-Freigaben für die
  Spiel-EXEs schon gesetzt (Regeln `TW1MP - TwoWorlds*.exe`), und die
  LAN-Adresse ist bekannt. Das Windows-DirectPlay bewirbt zuerst den
  Phantom-Adapter 192.168.0.58 und erst danach 192.168.1.136, das war am
  19.7. kein Hindernis.
- **Surface tritt bei.** Es braucht keine eingehende Freigabe.

## Ablauf

1. Beide: Spiel starten, einloggen, dieselbe Stadt betreten, kurz warten,
   bis man sich sieht.
2. Haupt-PC: Spiel erstellen (Einstieg neuer Spieler möglich anhaken),
   noch nicht starten.
3. Surface: Sitzung in der Liste markieren, Teilnehmen.
4. Haupt-PC: Spiel starten. Beide laden die Map.
5. In der Map, so viel wie in wenigen Minuten geht:
   - zueinander laufen, sich gegenseitig sehen
   - einmal chatten
   - einen Gegner angreifen, ein Item aufheben
   - wenn es noch läuft: Surface verlässt die Sitzung regulär, danach
     Haupt-PC beendet das Spiel. Ein Absturz ist auch in Ordnung.
6. Logs einsammeln, bevor jemand das Spiel erneut startet (das Log wird
   sonst fortgeschrieben):
   - Haupt-PC: `<Ordner>\dpnetcapture.log` nach
     `FoxShare\TW1MP-Capture\logs\main-<Datum>.log`
   - Surface: `<Ordner>\dpnetcapture.log` nach
     `FoxShare\TW1MP-Capture\logs\surface-<Datum>.log`
7. Danach optional den Shim wieder abschalten: `.\register.ps1 -Remove`.

Absturzsignatur wie gehabt: Application-Eventlog, `TwoWorlds*.exe` mit
`dpnet.dll`. Der Shim taucht dort nicht als Modul auf, er ruft nur durch.

## Was aus dem Log beantwortet wird

Diese Fragen sind heute offen und entscheiden, was die Relay-DLL genau
tun muss:

- Reihenfolge beim Beitritt: `EnumHosts`, `ENUM_HOSTS_RESPONSE`, `Connect`,
  `INDICATE_CONNECT` beim Host, `CREATE_PLAYER` für wen in welcher Folge,
  `CONNECT_COMPLETE` davor oder danach.
- Welche `dpnid`-Werte das Spiel bekommt und ob es sie irgendwo vergleicht
  (`GetPeerInfo`, `GetPlayerContext`, `EnumPlayersAndGroups`).
- `SendTo`: Ziel (0 = alle, oder ein bestimmter Spieler), Flags
  (`GUARANTEED`, `NOLOOPBACK`, `NONSEQUENTIAL`), Größe, Häufigkeit.
- Ob das Spiel seine eigenen Broadcasts zurückerwartet (Loopback).
- Welche Methoden erst mit zweitem Spieler auftauchen und was das Spiel
  mit deren Rückgabewert macht. Alles, was heute Stub ist, steht hier auf
  dem Prüfstand.
- Verlassen und Abbruch: `DESTROY_PLAYER`-Gründe, `TERMINATE_SESSION`,
  wer wann `Close` ruft.
- Rückgabewerte der Handler: `RECEIVE` mit `PENDING` plus `ReturnBuffer`
  oder direkt `S_OK`.

Selbsttest ohne Spiel: `build-test.bat` und `test_capture.exe` im
Ordner `directplay-capture` hosten über den Shim eine Sitzung auf dem
Windows-DirectPlay und prüfen das Log auf die nötigen Details.
