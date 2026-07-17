# Handoff — Lokaler Test mit dem echten Spiel

Stand: 2026-07-17 · Branch `claude/two-worlds-multiplayer-server-otqmlr` · [PR #1](https://github.com/MedievalDev/Tw1mp/pull/1)

## Status

- Server komplett implementiert (Fortführung von buglords CC0-Referenzserver
  [TW1CS 0.2.0](https://github.com/buglord/Two-Worlds-1-Misc-Projects/tree/main/Lobby%20Server)).
- 29 automatisierte Tests grün, inklusive simuliertem Spiel-Client auf
  Protokollebene (`tests/tw1_client.py`).
- **Noch nie gegen den echten Spiel-Client getestet** — genau das ist der
  nächste Schritt. Alles Client-seitige unten ist aus buglords Doku/Code
  abgeleitet, nicht selbst verifiziert.

## Code-Überblick

| Datei | Inhalt |
|---|---|
| `TW1MP-Server.py` | Launcher (`--help` für Optionen) |
| `tw1mp/protocol.py` | Binärprotokoll: Handshake-/Login-Pakete, Delphi-Strings, `gen64`-Serial-Antwort |
| `tw1mp/server.py` | TCP-Server, Login-Phase, Lobby-Loop, Nachrichtenverteiler-Thread |
| `tw1mp/commands.py` | Alle `/…`-Textkommandos der Lobby |
| `tw1mp/lobby.py` | User, ChatChannel, GameChannel (Städte), GameEntry (gehostete Spiele) |
| `tw1mp/database.py` | SQLite-Accounts (Schema identisch zu TW1CS v1) + PlayerData-Dateien |
| `tw1mp/config.py` | `Config.ini` (wird beim ersten Start erzeugt) |
| `tw1mp/web.py` | Optionales HTTP-Status-API (standardmäßig aus) |
| `tests/` | Protokoll-, Datenbank- und End-to-End-Tests |

## Lokal starten

Python 3.10+ reicht, keine Pakete nötig:

```sh
python TW1MP-Server.py            # Port 17171, erzeugt Config.ini etc.
python TW1MP-Server.py -l DEBUG   # gesprächiges Log für den Spiel-Test
python -m unittest discover -s tests   # Testsuite
```

Beim ersten Start entstehen im Arbeitsverzeichnis: `Config.ini`,
`ServerData.db` (Accounts), `PlayerData/` (Spielstände). Löschen von
`ServerData.db` = frischer Server.

## Spiel mit dem Server verbinden

1. **Spielversion:** Two Worlds 1.7 (Steam „Epic Edition“). buglord hat mit
   dem Steam-Linux-Branch (`TW1LinuxBETA`, gebündeltes Wine) getestet.
2. **DNS-Umleitung:** In `C:\Windows\System32\drivers\etc\hosts` den
   offiziellen Lobby-Host auf deine Server-IP zeigen lassen (lokal `127.0.0.1`).
   ⚠️ **Größte offene Frage:** Welchen Hostnamen das Spiel exakt auflöst, ist
   nicht dokumentiert (historisch tauchte z. B. `hawk.2-worlds-us.com` auf).
   Am besten beim ersten Start einmal mit Wireshark o. ä. die DNS-Anfrage
   mitschneiden — oder prüfen, ob es analog zum `ActivationServer`-Eintrag
   einen Registry-Wert für den Lobby-Server gibt
   (`HKLM\SOFTWARE\WOW6432Node\Reality Pump\TwoWorlds\...`).
3. **Aktivierung:** Falls das Spiel vorher eine Online-Aktivierung verlangt,
   buglords „TW1 Local Activation Server“ benutzen (läuft auf Port 80,
   Windows, setzt den Registry-Pfad selbst um).
4. **Firewall/Ports:** TCP 17171 freigeben (plus 17071, falls Web-API an).
5. **Spielsitzung selbst:** läuft nach dem Lobby-Start **Peer-to-Peer über
   DirectPlay** zwischen den Clients. Auf modernem Windows ggf. die
   Legacy-Komponente „DirectPlay“ aktivieren (Systemsteuerung → Windows-Features).
   Über Internet sind dafür Portfreigaben beim Host oder ein VPN nötig;
   für den ersten Test am besten zwei Clients im selben LAN.

## Test-Checkliste mit dem echten Client

- [ ] Verbindungsaufbau; bei leeren gespeicherten Zugangsdaten muss der
      Login-/Registrierungsdialog erscheinen (Server schickt dafür bewusst
      ein Fehlerpaket)
- [ ] Registrierung (E-Mail/Ort/Alter/Beschreibung) und erneuter Login;
      MOTD/Servertitel korrekt angezeigt
- [ ] Serial-Bindung: zweiter Account mit gleichem Key wird abgelehnt
      (`bind_serial = true` in `Config.ini`)
- [ ] Städteliste (4 Kanäle), Stadt betreten, zweiter Spieler sichtbar
      (Held + Positionsbewegung auf der Karte)
- [ ] Chat main/trade inkl. Kanalwechsel und Umlauten
- [ ] `/whois` auf anderen Spieler; eigene Profildaten im Spiel ändern
- [ ] Spiel erstellen → erscheint beim zweiten Client → beitreten
      (auch mit Passwort) → starten → DirectPlay-Session kommt zustande
- [ ] Nach Spielende: Spielstand wird hochgeladen und beim nächsten Login
      wieder geladen (Dateien unter `PlayerData/`)
- [ ] Sauberes Verhalten bei Alt+F4 / Verbindungsabbruch eines Clients

**Bitte notieren:** Jede Server-Logzeile `Unknown command from …` — das sind
Kommandos, die der echte Client sendet, aber weder Doku noch Referenzserver
kennen. Die sind Gold wert fürs Nachrüsten.

## Bewusste Annahmen (beim Test im Blick behalten)

- `getguildrankpoints`/GRP: feste Werte wie im Referenzserver (Seed 0) —
  laut Doku „scheint zu funktionieren“, nötig für Spielstart.
- `/startgame` ist serverseitig ein No-op; der Statuswechsel passiert über
  `/startinggame` (wie in TW1CS).
- `/nick` wird höflich abgelehnt (Umbenennen würde Account-/Lobby-Keys brechen).
- Spielstands-Zugriff (`/getplayerdata`, `/setplayerdata`) nur auf den
  eigenen Namen (TW1CS hatte Fremdzugriff als offenes TODO).
- Leerer Username beim Auto-Login → Fehlerpaket → Client zeigt Login-Prompt.

## Troubleshooting beim Test

- `Config.ini`: `allow_any_login = true` schaltet die Passwortprüfung ab
  (nur zum Debuggen), `auto_register = true` (Standard) legt unbekannte
  Nutzer beim ersten Login automatisch an.
- Web-API einschalten (`[Web] enabled = true`, `debug_api = true`):
  `http://localhost:17071/status` und
  `http://localhost:17071/debug?lists=player+town+game` zeigen Live-Zustand.
- Referenzmaterial zum Abgleich: `TW1CS.py` und
  `TwoWorlds1LobbyProtocol.html` im
  [Upstream-Repo](https://github.com/buglord/Two-Worlds-1-Misc-Projects)
  („Lobby Server“-Ordner).

## Danach

Befunde einfach als PR-Kommentare oder hier in die Session geben — ich
arbeite sie ein. Wenn der Test mit echtem Client durch ist: PR aus dem
Draft-Status nehmen, dann Betrieb (systemd/Docker, Backups von
`ServerData.db` + `PlayerData/`, öffentliche Instanz) als Folgeschritt.
