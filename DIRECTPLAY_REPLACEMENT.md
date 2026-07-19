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

## Maßgebliche Referenzen (verifiziert)

- **COM-Interface-Layout:** Wine `include/dplay8.h` (LGPL). Liefert die
  exakte Vtable-Reihenfolge. `IDirectPlay8Peer` = 37 Slots:
  QueryInterface/AddRef/Release, dann Initialize, EnumServiceProviders,
  CancelAsyncOperation, Connect, SendTo, GetSendQueueInfo, Host,
  GetApplicationDesc, SetApplicationDesc, CreateGroup, DestroyGroup,
  AddPlayerToGroup, RemovePlayerFromGroup, SetGroupInfo, GetGroupInfo,
  EnumPlayersAndGroups, EnumGroupMembers, SetPeerInfo, GetPeerInfo,
  GetPeerAddress, GetLocalHostAddresses, Close, EnumHosts, DestroyPeer,
  ReturnBuffer, GetPlayerContext, GetGroupContext, GetCaps, SetCaps,
  SetSPCaps, GetSPCaps, GetConnectionInfo, RegisterLobby, TerminateSession.
  Wine `dlls/dpnet/*.c` ist für die Session-Logik nur Stub — nicht als
  Transport-Vorlage brauchbar.
- **Wire-Protokoll (das Entscheidende):** Microsoft Open Specifications
  dokumentieren das komplette DirectPlay8-Protokoll:
  - [MC-DPL8R] „DirectPlay 8 Protocol: Reliable" — UDP-Zuverlässigkeit:
    Sequenznummern, ACKs, Sliding Window, reliable/unreliable ×
    sequential/nonsequential.
  - [MC-DPL8CS] „Core and Service Providers" — Session-Aufbau
    (EnumHosts, Connect, Host), Player-Management.
  Damit ist Interop mit echter dpnet.dll grundsätzlich möglich (nicht nur
  „beide installieren die Ersatz-DLL"). Das hebt das Projekt von
  „Blackbox reverse-engineeren" auf „gegen dokumentierte Spec bauen".

## Verifizierter Ladepfad (per Shim-Prototyp bestätigt)

Der Interception-Shim wurde erfolgreich geladen, als er unter
`HKCU\Software\Classes\WOW6432Node\CLSID\{…}\InprocServer32` registriert
war (NICHT unter dem plain-CLSID-Pfad — der 32-bit-Prozess liest den
WOW6432Node-View). Ground-Truth-Mitschnitt bestätigte:
- Two Worlds erzeugt beim **Programmstart** ein `CLSID_DirectPlay8Peer`
  und fragt `IID_IDirectPlay8Peer = {5102DACF-241B-11D3-AEA7-006097B01411}`
  an (also Peer-to-Peer, kein Client/Server).
- Direkt nach Erzeugung: AddRef/Release-Refcount-Tanz.
Der generische ASM-Thunk-Ansatz des Prototyps ist zu fragil (crasht das
Spiel) — die echte Implementierung nutzt **handgeschriebene C++-Methoden
in exakter Vtable-Reihenfolge**, kein generischer Thunk.

## Experten-validierter Bauplan (4 unabhängige Reviews, hohe Übereinstimmung)

### Harte Regeln (Verstoß = Crash oder stiller Fehler)

1. **Vtable NIEMALS von Hand bauen.** Kanonischen `dplay8.h`/`dpaddr.h`
   nehmen (mingw-w64 = permissiv lizenziert für das verteilte Binary;
   Wine = LGPL, nur für lokale Tools/Studium). Interface als C++-Klasse
   aus dem Header, MSVC erzeugt die Vtable. ALLE ~37 Peer-Slots müssen
   existieren, ungenutzte als Stub mit `return DPNERR_UNSUPPORTED;` — ein
   fehlender/vertauschter Slot ruft die falsche Methode und crasht sofort.
2. **Peer und Address getrennte C++-Objekte** (keine Mehrfachvererbung auf
   einer Klasse). IUnknown/Refcount strikt korrekt. QueryInterface muss
   `IID_IDirectPlay8Peer {5102DACF-241B-11D3-AEA7-006097B01411}` liefern.
3. **`ThreadingModel = Both`** in InprocServer32 (sonst COM-Apartment-
   Marshaling → E_NOINTERFACE/Crash beim CoCreateInstance). register.ps1
   setzt das bereits.
4. **Message-Handler-Contract** (`PFNDPNMESSAGEHANDLER`, WINAPI/__stdcall,
   3 Args, Rückgabewert ist semantisch tragend):
   - `DPN_MSGID_INDICATE_CONNECT` → Host akzeptiert die Verbindung durch
     Rückgabe `S_OK` (FEHLTE in der ersten Liste, handshake-kritisch).
   - `CREATE_PLAYER` / `DESTROY_PLAYER`, `CONNECT_COMPLETE`, `RECEIVE`,
     `ENUM_HOSTS_RESPONSE`, `TERMINATE_SESSION`.
   - Jede `DPNMSG_*`-Struct braucht korrektes `dwSize` — App liest sonst
     an falschen Offsets (stiller Crash tief im Spielcode).
   - `RECEIVE`-Puffer-Ownership: `S_OK` (sofort fertig) vs.
     `DPNSUCCESS_PENDING` + späteres `ReturnBuffer` — falsch = use-after-
     free.
5. **Threading:** Handler läuft reentrant auf Worker-Threads. NIE ein Lock
   über den App-Callback halten (reentrantes SendTo/Close aus dem Handler
   → Deadlock). Ein geordneter Dispatch-Thread hält die RECEIVE-Reihenfolge
   pro Sender.
6. **Port-Bindung:** Host MUSS exakt den in der Device-Address / im
   Lobby-Announce genannten Port binden (z. B. 57033), sonst kann der
   zweite Client nicht connecten.

### Großer Aufwands-Reduzierer

Definition-of-Done = LAN-Session zwischen 2 Clients, **beide mit der
Ersatz-DLL**. Damit ist **keine Wire-Kompatibilität** zum MS-DirectPlay
nötig — das UDP-Format darf beliebig sein (eigenes Framing + Sequenz +
ACK). Der volle MC-DPL8R-Reliability-Layer, DPNSVR (UDP 6073) und das
DPN-Framing sind KEIN Reimplementierungsziel. Reduziert den Netzwerkteil
von „Monate Protokoll-RE" auf „ein ordentlicher Reliable-UDP-Layer".
(MS-Wire-Protokoll nur nötig, falls Interop mit Stock-DirectPlay-Nutzern
gewünscht ist — separates, optionales Ziel.)

### Minimal zu implementierende Peer-Methoden

`Initialize`, `EnumServiceProviders` (2 Modi: SP-Liste mit
CLSID_DP8SP_TCPIP + Device-Liste), `Host`, `Connect`, `EnumHosts`,
`SendTo`, `GetSendQueueInfo`, `GetCaps`/`SetCaps`, `GetPeerInfo`/
`SetPeerInfo`, `GetPlayerContext`, `Close`, `GetApplicationDesc`. Rest als
`DPNERR_UNSUPPORTED`-Stub. `IDirectPlay8Address`: `BuildFromURLW`,
`GetComponentByName`, `SetSP`, `GetSP`, `AddComponent` (URL-Parsing der
`x-directplay:/…`-Adresse; Wine `dlls/dpnet/address.c` ist hier
tatsächlich implementiert und als Semantik-Vorlage brauchbar).

### Reihenfolge

0. **Ground-Truth-Mitschnitt** (Phase 0, Prerequisite): Interception-Shim
   korrekt bauen (Bug: generischer Thunk ersetzte den `this`-Zeiger nicht
   → echte dpnet bekam `this=Proxy*` → Crash. Fix: pro Methode ein echter
   C++-Forwarder aus dem Header, kein generischer ASM-Thunk; zusätzlich in
   `Initialize` den Message-Handler-Callback wrappen, um die reale
   Nachrichten-Sequenz zu sehen). Läuft gegen die aktuell funktionierende
   Windows-dpnet → liefert die exakte Methoden-/Nachrichten-Reihenfolge,
   die Two Worlds real nutzt. Verhindert Raten.
1. COM-DLL-Skelett: alle Interfaces mit compiler-erzeugter Vtable, alle
   Methoden zunächst Stub. Laden + CoCreateInstance ohne Crash verifizieren.
2. Address-URL-Parsing + EnumServiceProviders (Spiel muss den TCP/IP-SP
   sehen, sonst scheitert es vor Host/Connect).
3. Reliable-UDP-Layer (eigenes Framing) + Host/Connect/EnumHosts-Handshake.
4. Message-Handler-Dispatch (INDICATE_CONNECT, CREATE_PLAYER,
   CONNECT_COMPLETE, RECEIVE) → LAN-Test 2 Clients, Gameplay lädt.
5. Optional: NAT-Resolver für Internet-MP ohne VPN.

### Aufwand & Empfehlung

Einhellig: **mehrwöchiges Projekt**, hohes Korrektheitsrisiko am
Vtable-/Message-Contract; braucht 2-Maschinen-LAN-Test. MP läuft aktuell
mit Stock-dpnet nach dem DISM-Fix — einziger Treiber ist Future-Proofing
(Windows könnte DirectPlay künftig entfernen; ist noch optionales Feature
in Win11). Phasen 0–2 sind risikoarm und liefern die tragfähige Basis;
Phasen 3–4 sind der eigentliche Netzwerk-Kern und brauchen iteratives
Testen mit zwei echten Clients.

## Phase 0 ABGESCHLOSSEN — Ground-Truth-Mitschnitt (verifiziert)

Der korrigierte Capture-Shim (`directplay-shim/dpnetshim.cpp`, compiler-
erzeugte Vtable aus dem Wine-Header, kein ASM-Thunk, Message-Handler in
`Initialize` gewrappt) lief **stabil durch eine komplette MP-Session** —
Login, Stadt, F12→F12, Map geladen und spielbar. Damit ist bewiesen:
COM-Interception + korrekte Vtable + Message-Handler-Wrapping tragen einen
echten Session-Aufbau.

**Mitgeschnittene Solo-Host-Sequenz (genau das, was F12 auslöst):**

```
CoCreateInstance(Peer) -> Initialize(handler, flags=0)
EnumServiceProviders  (2x: Größenabfrage, dann Füllen -> muss TCP/IP-SP liefern)
GetSPCaps
SetPeerInfo           (lokaler Spielername)
Host                  -> Message CREATE_PLAYER (lokaler Spieler)
GetPeerInfo (2x), GetLocalHostAddresses
[Gameplay-Schleife]   SendTo -> Message SEND_COMPLETE + Message RECEIVE
                      (Solo: an sich selbst; jeder RECEIVE-Puffer via ReturnBuffer)
Close                 -> Message DESTROY_PLAYER
```

**Nur 10 Peer-Methoden real genutzt (Solo-Host):** Initialize,
EnumServiceProviders, GetSPCaps, SetPeerInfo, Host, GetPeerInfo,
GetLocalHostAddresses, SendTo, ReturnBuffer, Close. **4 Nachrichten:**
CREATE_PLAYER, DESTROY_PLAYER, RECEIVE, SEND_COMPLETE. Alle übrigen ~27
Peer-Slots bleiben `DPNERR_UNSUPPORTED`-Stubs.

`Connect`, `EnumHosts` und die Nachrichten INDICATE_CONNECT/
CONNECT_COMPLETE tauchen im Solo-Fall NICHT auf — sie kommen erst dazu,
wenn ein zweiter Client der Session beitritt (Phase 3/4, 2-Maschinen-Test).

## Phase 1 — nächster Schritt (einzeln testbar)

Minimales Replacement-DLL, das die 10 Solo-Host-Methoden implementiert und
die 4 Nachrichten liefert, mit **Loopback** (SendTo an die eigene Session
kommt als RECEIVE zurück) — noch OHNE echtes Netzwerk. Ziel: ein
**Solo-Spieler startet eine Map über UNSERE DLL** auf einer Maschine.
Das ist der erste Meilenstein, den ich allein verifizieren kann. Danach
Phasen 3/4 (echtes UDP + 2. Client) mit zweitem Test-Rechner.

## Phase 1 ABGESCHLOSSEN — Solo-Host-Replacement läuft (verifiziert)

`directplay-replace/dpnetreplace.cpp` ist eine echte DirectPlay8-Ersatz-DLL
(kein Shim): eigene Implementierungen von `IDirectPlay8Peer` (die 10
genutzten Methoden + 24 `DPNERR_UNSUPPORTED`-Stubs) und
`IDirectPlay8Address` (SP/Komponenten/URL-Bau), mit Loopback-Message-
Dispatch auf einem Worker-Thread. Registrierung admin-frei per-user unter
`WOW6432Node` (`register.ps1`).

**Live gegen den echten Client verifiziert (2026-07-18):** Mit registriertem
Replacement (stock dpnet komplett umgangen) durchlief das Spiel die gesamte
Host-Setup-Sequenz über unseren Code — `Initialize`, `EnumServiceProviders`
(beide Calls), `Address::SetSP`/`AddComponent`, `GetSPCaps`,
`SetPeerInfo(marco19942)`, `Host → local player 1`,
`GetLocalHostAddresses`, `GetPeerInfo` — und lud eine spielbare
Multiplayer-Map. Charakter live und beweglich, Quest aktiv, **kein Crash**
(letzter WER-Crash lag vor dem Test). Der komplette DirectPlay-Aufbau läuft
damit ohne Windows-Legacy-dpnet.

Vor dem Live-Test lief eine adversariale Multi-Agent-Review; der einzige
reale Befund (Worker-Thread-Lebensdauer: Use-after-free bei
Release-ohne-Close und beim 2s-Join-Timeout) wurde behoben (Destruktor
stoppt den Worker, Join unbegrenzt).

Nicht ausgelöst in diesem Solo-Map-Lauf: der SendTo→RECEIVE-Loopback — in
einer Solo-Spielmap ohne weitere Peers sendet der Client nichts (korrektes
Verhalten). Der Pfad ist implementiert und reviewt; die Stadt-
Positions-Broadcasts bzw. der 2-Spieler-Fall werden ihn ausüben.

## 2-Maschinen-Test (LAN, stock DirectPlay) — Ergebnis

Am 2026-07-19 mit zwei echten Rechnern im LAN getestet (Haupt-PC +
Surface Pro 7), beide über den TW1MP-Lobby-Server:

- **Lobby + Matchmaking: voll funktionsfähig.** Beide loggen sich ein
  (verschiedene Namen, gleicher Key dank `bind_serial=false`), sehen sich
  in derselben Stadt inkl. gegenseitiger Positionsbewegung, einer erstellt
  ein Spiel, der andere sieht und tritt bei, die DirectPlay-Adresse wird
  übergeben. **Beide waren gemeinsam in der Mission-Map** — der komplette
  Community-Server-Stack ist damit end-to-end validiert.
- **Aber: stock `dpnet.dll` ist instabil.** Der Host crasht teils schon
  beim Erstellen (`/requestcreategame` → sofort disconnect), und die
  Surface ist mitten im gemeinsamen Spiel abgestürzt — beide Male
  bestätigt `APPCRASH … dpnet.dll`. Der DISM/sfc-Fix hat die 32-bit-dpnet
  nie wirklich getauscht; sie bleibt auf modernem Windows unzuverlässig.
- **Adress-Fund:** Der Host bewirbt als *primäre* DirectPlay-Adresse ein
  Phantom `192.168.0.58` (kein Live-Adapter; wahrscheinlich Altlast eines
  Virtual-Adapters wie Hamachi), die korrekte LAN-IP `192.168.1.136` nur
  als `alt=`. Fatal war das nicht (DirectPlay verband über den Fallback),
  aber unsere Replacement-DLL wird die richtige IP direkt bewerben.

**Fazit:** 2-Spieler-Machbarkeit bewiesen; die Unzuverlässigkeit liegt
allein an stock DirectPlay. Der Weg zu *stabilem* 2-Spieler ist Phase 3 —
unsere Replacement-DLL um echtes UDP-Transport erweitern, dann hängt nichts
mehr an der kaputten Windows-dpnet.

## Offene Phasen

- **Phase 2:** SendTo→RECEIVE-Loopback in der Stadt ausüben (Positions-
  Broadcasts), um den Gameplay-Nachrichtenpfad end-to-end zu bestätigen.
- **Phase 3:** echtes UDP-Transport statt Loopback (WinSock2), damit ein
  zweiter Client beitreten kann — braucht zusätzlich `Connect`, `EnumHosts`
  und die Nachrichten INDICATE_CONNECT/CONNECT_COMPLETE (im Solo-Fall nie
  aufgerufen). 2-Maschinen-LAN-Test.
- **Phase 4:** optionaler eigener NAT-Resolver → Internet-MP ohne VPN.

## Phase 3 - UDP-Transport gebaut + selbst-verifiziert (2026-07-19)

Die Replacement-DLL kann jetzt zwei Peers ueber echtes UDP verbinden:
`Connect`/`EnumHosts` implementiert, Host bindet einen UDP-Socket und
bewirbt die KORREKTE LAN-IP (kein Phantom mehr), die beiden DLLs handshaken
(CONNECT/ACK), vergeben Spieler-IDs (Host=1, Joiner=2) und relayen
SendTo<->RECEIVE ueber UDP. Ein Selbsttest-Harness (test_p2p.cpp, zwei
Instanzen in einem Prozess) bestaetigt Handshake, beidseitige Spieler-
erzeugung und Nachrichtenaustausch OHNE das Spiel - Ergebnis PASS.
Naechster Schritt: 2-Maschinen-Test mit dem echten Client (beide Rechner
registrieren die Replacement-DLL).

## Phase 3+ - Reliability + Liveness (2026-07-19)

Ueber das rohe UDP-Transport gelegt, damit die Verbindung praxistauglich wird:

- **Reliable ordered delivery fuer `DPNSEND_GUARANTEED`.** Jedes reliable
  Paket traegt eine Sequenznummer; der Empfaenger ACKt jedes (PKT_DATA_ACK),
  puffert out-of-order Pakete (m_reorder), verwirft Duplikate und liefert
  streng in Reihenfolge aus (m_rSeqInExpected). Der Sender haelt unbestaetigte
  Pakete (m_unacked) und `RetransmitTick` sendet nach 150ms RTO neu (max 40
  Versuche, ~6s). Unreliable Sends gehen weiter best-effort direkt raus.
- **Keepalive + Dead-Peer-Timeout.** Ein abgestuerzter Peer sendet nie BYE
  (genau der Fall „Surface zwischendrin abgeschmiert"). PKT_PING alle 2s;
  bleibt der Peer >10s still, synthetisiert der Net-Thread `DESTROY_PLAYER`
  und verwirft den Reliable-State — das Spiel merkt sauber, dass der Partner
  weg ist, statt an einer toten Verbindung zu haengen.

Der Selbsttest (test_p2p.cpp) deckt jetzt vier Phasen ab und ist gruen
(15/15 im Soak): Handshake, beidseitiges Messaging, **50 garantierte
Nachrichten unter 30% Paketverlust — alle exakt einmal, in Reihenfolge**,
und **Dead-Peer-Erkennung** (100% Verlust inkl. Keepalives + kurzer Timeout
ueber die Test-Hooks DpnTestSetDrop/DpnTestSetTimeout → Host feuert
DESTROY_PLAYER).

Ein adversarialer Review dieses neuen Codes fand einen HIGH- und mehrere
MED-Bugs, alle behoben: (H1) Retransmit-Aufgabe (~6s) riss den garantierten
Kanal still ab statt die Verbindung sauber zu beenden → jetzt gemeinsamer
`DropPeer`-Teardown; (M1) Seq-Zaehler wurden bei Reconnect nicht
zurueckgesetzt → Reset in `DropPeer` und beim Verbindungsaufbau; (M2)
RECEIVE nach falschem DESTROY → PKT_DATA gated auf lebenden Peer; (M3)
unbeschrankter Reorder-Puffer → Empfangsfenster (RECV_WINDOW=256, ausserhalb
weder puffern noch acken); (M4) Race auf `m_hasPeer` → `volatile LONG` mit
atomarem Test-and-Clear. Dazu: StartNet/StartWorker idempotent, `m_wake`
TOCTOU beim Teardown geschlossen.

Stand: 2026-07-19. Scoping + Feasibility + Ladepfad + Phase 0
(Ground-Truth) + **Phase 1 (Solo-Host-Replacement, live verifiziert)** +
**Phase 3 (UDP-Transport, Reliability, Liveness — selbst-verifiziert)**
abgeschlossen. Offen: 2-Maschinen-Test mit dem echten Client.
