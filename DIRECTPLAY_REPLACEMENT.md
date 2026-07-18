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

Stand: 2026-07-18. Scoping + Feasibility + Ladepfad verifiziert,
Experten-Review eingearbeitet. Implementierung: Skelett noch nicht gebaut.
