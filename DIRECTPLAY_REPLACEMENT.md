# DirectPlay Replacement — Scoping & Plan

Ziel (Marco): DirectPlay durch etwas Zukunftssicheres ersetzen, das ohne
Windows-Legacy-Feature auskommt und **einfach zu verwenden** ist (idealerweise
eine Datei neben die Spiel-EXE legen, kein Admin).

## Warum überhaupt

Der Multiplayer-Spielstart crasht den Client in `dpnet.dll` (DirectPlay8),
**byte-identisch gegen unseren Server und den offiziellen**. Der Windows-Standard-
Fix wurde durchgeführt und schlug fehl:
- DirectPlay-Feature war bereits `Enabled` (auch LegacyComponents).
- `DISM /Online /Cleanup-Image /RestoreHealth` + `sfc /scannow` liefen sauber
  durch, fanden nichts zu reparieren.
- Die 32-bit `SysWOW64\dpnet.dll` blieb auf `10.0.22621.1` (RTM), die 64-bit auf
  `.4599` — die Differenz besteht weiter.
- Crash reproduziert danach erneut: WER `APPCRASH … dpnet.dll` (18.07. 16:13).

→ Der Standard-Weg ist ausgeschöpft. Das Replacement ist damit nicht mehr
optional, sondern der Weg, um MP auf diesem (und modernen) Windows zu erhalten.

## Wie das Spiel DirectPlay lädt (verifiziert per PE-Import-Analyse)

`TwoWorlds_RADEON.exe` (32-bit) importiert **kein** `dpnet.dll` statisch.
Stattdessen:
- `ole32.dll`: `CoInitializeEx`, `CoCreateInstance`, `CoTaskMemAlloc/Free`, …
- `WS2_32.dll`: Winsock (per Ordinal) — für eigene Netzwerknutzung.
- Service-Provider-GUID im Lobby-Announce gesehen:
  `{EBFE7BA0-628D-11D2-AE0F-006097B01411}` = **CLSID_DP8SP_TCPIP**.

Das heißt: Das Spiel instanziiert DirectPlay8 über **COM/CoCreateInstance** mit
den Standard-CLSIDs. dpnet.dll ist ein COM-Inproc-Server, registriert unter
`HKLM\SOFTWARE\Classes\CLSID\{…}\InprocServer32 → System32\dpnet.dll`.

Konsequenz: Eine lokale `dpnet.dll` neben der EXE wird von COM **nicht**
automatisch geladen (COM löst über die Registry-CLSID auf, nicht über den
DLL-Suchpfad). Umleitung braucht einen der folgenden Wege.

## Deployment-Optionen (alle admin-frei möglich)

1. **SxS-Manifest** neben der EXE (`TwoWorlds_RADEON.exe.manifest`), das die
   DirectPlay8-CLSIDs auf eine lokale Ersatz-DLL umbiegt (registry-free COM).
   → Am nächsten an „eine Datei danebenlegen".
2. **Per-User-Registrierung** unter `HKCU\Software\Classes\CLSID\{…}` auf die
   Ersatz-DLL. Kein Admin nötig, überschreibt HKLM für diesen Nutzer.
   → Kann das TW1MP-Tool per Knopf setzen/entfernen.

## Zu implementierende COM-Interfaces (DirectPlay8, nur genutzter Teil)

Two Worlds nutzt Peer-to-Peer + einen dedizierten Host. Minimal nötig:
- `IDirectPlay8Peer` (Host + Client der Session), CLSID_DirectPlay8Peer
  `{286F484D-375E-4458-A272-B138E2F80A6A}`
- `IDirectPlay8Address` (die x-directplay-URL), CLSID_DirectPlay8Address
- ggf. `IDirectPlay8Client` / `IDirectPlay8Server`
- `IDirectPlay8SP` TCP/IP `{EBFE7BA0-…}`
Kern-Methoden: `Initialize`, `EnumServiceProviders`, `Host`, `Connect`,
`EnumHosts`, `SendTo`, `GetSendQueueInfo`, `Close`, plus das Message-Callback
(`PFNDPNMESSAGEHANDLER`) mit den Events (CREATE_PLAYER, RECEIVE, TERMINATE …).
Backend: moderne UDP-Sockets (WS2_32), optional eigener NAT-Resolver →
Internet-MP ohne Hamachi.

Referenz: Wine implementiert dpnet frei (LGPL) — `dlls/dpnet/` in der
Wine-Quelle. Als Vorlage für Interface-Layout & Semantik nutzbar; die
Transport-Ebene modern neu bauen.

## Reihenfolge

1. **Zuerst** MP mit einer funktionierenden DirectPlay-Referenz zum Laufen
   bringen (nötig, um das Replacement dagegen zu testen). Kandidaten, die
   VOR dem großen Projekt noch zu prüfen sind:
   - Warum ist die 32-bit dpnet auf RTM? Feature einmal *deaktivieren →
     Neustart → wieder aktivieren* erzwingt evtl. Neu-Staging der 32-bit-
     Payload (DISM/sfc tun das nicht).
   - Exakten Crash-Offset in dpnet.dll aus dem WER-Dump ziehen (welche
     Funktion genau) — schärft die Ursache.
2. Danach: COM-Interception-Prototyp (SxS oder HKCU) mit einer Stub-DLL, die
   nur `EnumServiceProviders`/`Initialize` beantwortet → prüfen, dass das
   Spiel unsere DLL überhaupt lädt.
3. Dann iterativ die Session-Methoden über UDP implementieren, LAN-Test mit
   zwei Clients, dann NAT-Resolver.

Stand: 2026-07-18. Scoping verifiziert, Implementierung noch nicht begonnen.
