# Website-Update: twmp.alchemy-fox.de

Handoff für den Chat, der die Website betreut. Der Lobby-Server hat seit dem
letzten Stand deutlich mehr Funktionen, und der neue **Admin-Bot verweist im
Spiel aktiv auf die Website** — die dort erwarteten Inhalte müssen also da sein.

**Repo:** https://github.com/MedievalDev/Tw1mp
**Branch:** `claude/two-worlds-multiplayer-server-otqmlr`
**Aktuelle Version:** 1.8.1

---

## 1. Warum das dringend ist

Der Admin-Bot steht dauerhaft in der Lobby und schickt bei jeder Begrüßung:

> Willkommen, &lt;Name&gt;! Tippe !help fuer alle Befehle.
> Infos, Mods & Anleitungen: https://twmp.alchemy-fox.de/

Und auf `!settings` bzw. `!commands` verweist er auf die Website als Quelle für
die **vollständige set.txt** und die **Konsolenbefehlsliste**. Diese beiden
Seiten sollten existieren, sonst laufen Spieler ins Leere.

---

## 2. Seiten, die es geben muss

### 2.1 `/set-txt` — Grafik-Tuning (vom Bot verlinkt, Priorität 1)

Der Bot nennt nur eine Kurzfassung. Auf der Seite gehört die **komplette,
kopierbare Datei** plus Erklärung, wohin sie gehört
(`<Spielordner>\set.txt`, wird beim Spielstart gelesen).

```
Engine.AlphaFadeNear 1500
Engine.AlphaFadeFar 3000
Engine.DLandAFade 1500
Engine.DLandFarClipp 7000
Engine.DLandFarClippOBJ 7000
Engine.DLandFogFarScale 120000
Engine.FarPlane 2500
Engine.LOD0 3200
Engine.LOD1 6400
Engine.LODblend 1066
Engine.SFarRng 1600
Engine.GFadeNear 1400
Engine.GFadeFar 1600
Engine.GrassDisp 28
Engine.GrassQ 0.4
Engine.GrassRandomizer 0
Engine.AutoDOFTolerance 0.6
```

Erklärtext dazu: deutlich mehr Sichtweite und Detail als der Auslieferungs-
zustand; `FarPlane` und `DLandFarClipp` sind die beiden mit dem größten
sichtbaren Effekt. Ein Vorher/Nachher-Screenshot wäre hier stark.

### 2.2 `/konsole` — Ingame-Befehle (vom Bot verlinkt, Priorität 1)

- Konsole öffnet mit `~`
- buglord hat **alle 1086 Befehle** aus der EXE extrahiert:
  https://github.com/buglord/Two-Worlds-1-Misc-Projects → `Commands/TwoWorldsCommands.txt`
- Cheat-Modus: `TwoWorldsCheats 1` (wird beim Neustart zurückgesetzt)
- Auf der Seite: die 20–30 nützlichsten Befehle als Tabelle, Rest verlinken.

### 2.3 `/server` — Mitspielen

- Serveradresse eintragen (Spiel → Netzwerk → Server hinzufügen), Port **17171**
- Hinweis: im selben WLAN die LAN-IP, von außen die öffentliche IP + Portfreigabe
- Registrierung passiert automatisch beim ersten Login
- Der **Admin-Bot** steht immer in der Lobby und beantwortet `!`-Befehle

### 2.4 `/downloads`

- Der Server selbst (Repo-Link, „Python installieren, doppelklicken")
- Die Mods aus dem Modding-Hub
- **QuestForge** (neu, siehe unten)

---

## 3. Neue Features seit dem letzten Website-Stand

Das gehört auf die Startseite bzw. eine Feature-Seite:

| Feature | Kurzbeschreibung |
|---|---|
| **Admin-Bot** | Steht immer in der Lobby, begrüßt Spieler, beantwortet `!help !players !uptime !server !web !discord !commands !settings` |
| **Server-UI** | Windows-Fenster mit Log, Spielerliste, Tray, Broadcast, Rechtsklick→Kick/Ban |
| **Settings-Tab** | Alle Server-Einstellungen im UI editierbar (Willkommensnachricht, Limits, Login-Policy) |
| **Savegame-Slots** | Mehrere Charakter-Stände pro Konto, umschaltbar; „New savegame" für frischen Charakter |
| **Charakter-Verwaltung** | Inventar ansehen (Cheat-Check), Items editieren, Charakter/Konto löschen |
| **Bans** | Sperre nach Name **und** Seriennummer, Registrierung sperrbar |
| **Charakter-Import** | Charakter vom offiziellen Server herunterladen und lokal importieren |

---

## 4. Text-Eingabe-Fix (eigene Seite wert)

Bekannter Two-Worlds-1-Bug auf Windows 10/11: **man kann in Textfelder kaum
tippen** (Login, Chat, CD-Key). Ursache ist ein falscher Nachrichtenfilter im
`PeekMessageA`-Aufruf des Spiels.

- Community-Fix: https://github.com/InsideTwoWorlds/Files → `Two Worlds/Fixes/Text Input/v1.7`
- Der patcht allerdings nur **einen bestimmten** Build; für andere Builds gibt es
  im Tw1mp-Repo `tools/tw_textinput_patch.py`, der beide ausgelieferten EXEs
  (`TwoWorlds.exe` und `TwoWorlds_RADEON.exe`) unterstützt, ein Backup anlegt und
  per `--restore` zurücksetzt.
- Workaround ohne Patch: Text woanders schreiben und mit Strg+V einfügen.

---

## 5. QuestForge — eigene Quests (neu, noch nicht veröffentlicht)

Werkzeug, das aus einer kleinen JSON-Datei eine spielbare Quest-Mod baut.
Liegt aktuell lokal unter `TwStuff\QuestForge\`, **noch nicht im Repo**.

Für die Website relevant, sobald der In-Game-Test bestätigt ist:
- Was es kann: neue Quests ohne WhizzEdit/Editor, nur zwei Dateien
- Ausführliche HTML-Doku liegt dem Werkzeug bei (`QuestForge_Guide.html`) —
  die könnte man weitgehend 1:1 als Website-Seite übernehmen
- **Status ehrlich kennzeichnen:** die Formate sind verifiziert, der Nachweis im
  laufenden Spiel steht noch aus. Nicht als fertig bewerben.

---

## 6. Offen / Rückfragen an Marco

1. **Discord-Link fehlt.** Der Bot hat einen `!discord`-Befehl, aber keine URL —
   er sagt derzeit ehrlich „noch nicht eingerichtet". Sobald der Link da ist:
   in `Config.ini` unter `bot_discord` eintragen (oder im Settings-Tab), und auf
   der Website verlinken.
2. Soll die Website die **öffentliche Serveradresse** nennen, oder bleibt der
   Server im LAN/privaten Kreis?
3. Screenshots: für set.txt (Vorher/Nachher) und das Server-UI wären welche gut.

---

## 7. Wo der Bot-Text herkommt

Falls Texte geändert werden sollen, damit Website und Bot konsistent bleiben:
`tw1mp/adminbot.py`, Abschnitt `_COMMANDS` — dort steht jeder `!`-Befehl mit
seiner Antwort. Website-URL und Discord kommen aus `Config.ini`
(`bot_website`, `bot_discord`), nicht aus dem Code.
