# dpnetrelay — DirectPlay8-Ersatz mit Relay-Transport

Nachfolger von `directplay-replace/` (das bleibt unverändert liegen). Gleiche
COM-Hülle, neuer Transport: die Spielsitzung läuft nicht mehr von PC zu PC,
sondern jeder Spieler, Host eingeschlossen, spricht **ausgehend per UDP mit
dem TW1MP-Relay** (`tw1mp/relay.py`), das die Pakete zwischen den
Mitgliedern einer Sitzung weiterreicht.

Was das ändert:

- Kein eingehender Port auf irgendeinem Spieler-PC, keine Firewall-Regel,
  kein VPN, keine LAN-IP, die stimmen muss. LAN und Internet laufen identisch.
- Bis zu 8 Spieler. Mitglieds-IDs vergibt das Relay (Host = 1, Beitretende
  bekommen die nächste Nummer, nie wiederverwendet) und dienen direkt als
  DirectPlay-Spieler-IDs.
- Ein Log für alles: das Relay sieht jeden Handshake; die DLL schreibt
  zusätzlich `dpnetrelay.log` neben sich (mitlesbar während das Spiel läuft).

## Dateien

| Datei | Zweck |
|---|---|
| `dpnetrelay.cpp` | die DLL: `IDirectPlay8Peer` + `IDirectPlay8Address` über das Relay |
| `build.bat` | baut `dpnetrelay.dll` (32-Bit, MSVC) |
| `register.ps1` | aktiviert die DLL per-user (HKCU, WOW6432Node, kein Admin); `-Remove`, `-Status`, `-Relay host:port` |
| `test_relay.cpp` / `build-test.bat` | Selbsttest: Host + zwei Beitretende in einem Prozess gegen ein laufendes Relay |
| `../tests/test_relay_dll.py` | startet ein Relay und lässt den Selbsttest laufen (`python -m unittest tests.test_relay_dll`) |

## Ablauf einer Sitzung

```
Host                          Relay                        Joiner
Host()  --RL_HELLO(host)-->   neue Session S, Host = 1
        <--RL_HELLO_OK(S,1)--
GetLocalHostAddresses -> x-directplay:/...;hostname=<relay>;port=17172;session=S
   (geht wie bisher über die Lobby an den Joiner)
                                                 EnumHosts -> ENUM_HOSTS_RESPONSE (sofort)
                              <--RL_HELLO(S,join)--  Connect()
                              --RL_HELLO_OK(S,id)-->
        <--IN_HELLO(name)-----(forward)----------  an alle (nur der Host wertet es aus)
INDICATE_CONNECT -> Spiel sagt ja (S_OK) oder nein (-1 + 24 Byte Antwortdaten)
 ja:   --IN_WELCOME(roster, Antwortdaten)-->  CREATE_PLAYER(self, host, andere), CONNECT_COMPLETE
       IN_JOINED(id, name) garantiert an die übrigen Spieler, dann CREATE_PLAYER(id) beim Host
 nein: --IN_REJECT(hr, Antwortdaten)------->  CONNECT_COMPLETE(HOSTREJECTEDCONNECTION + Antwortdaten)
       RETURN_BUFFER an den Host
   danach IN_DATA in beide Richtungen, RL_PING alle 2 s als Keepalive (das Relay schickt es zurück)
```

Innerhalb von `RL_DATA` sprechen die DLLs ihr eigenes kleines Protokoll
(`IN_HELLO`, `IN_WELCOME`, `IN_DATA` mit Sequenznummer, `IN_ACK`, `IN_BYE`,
`IN_REJECT`, `IN_KICK`, `IN_JOINED`, `IN_DROPPED`; Format oben in
`dpnetrelay.cpp`). Garantierte Sends (`DPNSEND_GUARANTEED`) sind pro
Spielerpaar zuverlässig und geordnet (Sequenz, Ack, Reorder-Fenster 256).
Jede garantierte Übertragung trägt einen Zeitstempel, den das Ack
zurückbringt; daraus kommt die Rundlaufzeit je Spieler
(`GetConnectionInfo`) und das Retransmit-Timeout (Rundlauf + 4 × Streuung,
100 ms bis 2 s, je Wiederholung verdoppelt bis 8×). Ungarantierte Sends
gehen sofort raus und werden nicht gepuffert, ein Send-Timeout (V9) läuft
darum nie ab. Loopback wie im Windows-DirectPlay: ein Send an die
Alle-Spieler-Gruppe kommt auch beim Absender an, außer mit
`DPNSEND_NOLOOPBACK` (mit `directplay-capture/test_capture.cpp` am echten
`dpnet.dll` nachgemessen).

Spieler kennt nur, wen der Host angekündigt hat (Roster im WELCOME oder
`IN_JOINED`). Daten von einer anderen Mitglieds-ID legen keinen Spieler an
und werden nicht bestätigt; eine garantierte Nachricht kommt dann wieder,
sobald das `IN_JOINED` da ist.

Rauswurf: `DestroyPeer(id, daten, 4)` am Host. Der Betroffene bekommt
`TERMINATE_SESSION` (HOSTTERMINATEDSESSION) mit genau diesen Daten in
`pvTerminateData` (das Spiel schickt 0x100000 oder 0x200000), danach
DESTROY_PLAYER für alle. Host und übrige Spieler bekommen `DESTROY_PLAYER`
mit HOSTDESTROYEDPLAYER (`IN_DROPPED`, garantiert). Wer abgewiesen oder
rausgeworfen ist und weiter sendet, bekommt dieselbe Antwort noch einmal.

Verlassen: `Close()` schickt `IN_BYE` + `RL_BYE`; die anderen bekommen
`DESTROY_PLAYER` (Grund NORMAL). Geht der Host, bekommen alle
`TERMINATE_SESSION` (HOSTTERMINATEDSESSION) und danach `DESTROY_PLAYER` für
jeden Spieler einschließlich sich selbst. Stiller Abbruch: das Relay meldet
nach 20 s Stille `RL_LEFT`, die DLL liefert `DESTROY_PLAYER` mit
CONNECTIONLOST; bleibt eine garantierte Nachricht 15 s unbestätigt, gilt der
Spieler ebenfalls als verloren (beim Beitretenden heißt das bei Spieler 1:
Sitzung beendet).

Relay-Ausfall: Antwortet ein Relay, das Keepalives zurückschickt, 15 s lang
nicht mehr, bekommt ein Beitretender `TERMINATE_SESSION` (CONNECTIONLOST).
Der Host behält sein eigenes Spiel und verliert nur die anderen Spieler,
wie bei Windows-DirectPlay. Ein älteres Relay ohne Echo wird nicht als
ausgefallen gewertet.

Neue NAT-Adresse: Kommen Pakete eines Mitglieds plötzlich von einem anderen
Port, antwortet das Relay mit `RL_ERROR 'rebind'`. Die DLL meldet sich mit
dem Token aus `RL_HELLO_OK` neu an (`RL_HELLO`, Rolle 3), behält ihre
Mitglieds-ID, und die garantierten Nachrichten laufen weiter.

## Relay-Adresse

Nur der **hostende** Spieler braucht sie, Beitretende lesen sie aus der
Sitzungsadresse. Reihenfolge in der DLL:

1. Umgebungsvariable `TW1MP_RELAY` (`host:port`)
2. Registry `HKCU\Software\TW1MP\Relay` (setzt `register.ps1 -Relay ...`)
3. Komponente `natresolver` am Host-Device (das Spiel setzt sie bei
   `EarthNet_UseNATResolver=1` und `EarthNet_AddNATResolverInHost=1`)
4. Registry `EarthNet_NATResolverAddress` unter
   `HKCU\SOFTWARE\Reality Pump\TwoWorlds\Network`
5. eingebaut: `87.106.168.34:17172` (Community-Server)

Bei 3 und 4 zählt nur der erste Eintrag einer Liste; eine Adresse mit
`2-worlds` (der offizielle Resolver, kein Relay) wird übersprungen.

Antwortet das Relay nicht, hostet die DLL trotzdem, aber solo: die Adresse
trägt `session=00000000`, ein Beitritt scheitert mit klarer Logzeile.

Das Log `dpnetrelay.log` liegt neben der DLL; jede Zeile trägt die
Prozess-ID (`p=`), damit zwei Spielinstanzen auseinanderzuhalten sind.

## Serverseite

`tw1mp/relay.py` läuft im Lobby-Server, wenn in `Config.ini` `[Relay]
enabled = true` steht (Port 17172/udp, im ufw freigeben), oder allein:

```
python -m tw1mp.relay --port 17172
```

Fürs Heimnetz reicht das lokal auf dem Haupt-PC; die Host-DLL dann mit
`.\register.ps1 -Relay 192.168.1.136:17172` darauf zeigen lassen.

## Was das Spiel bekommt

Implementiert: `Initialize`, `EnumServiceProviders`, `GetSPCaps`/`SetSPCaps`,
`GetCaps`/`SetCaps`, `SetPeerInfo`, `GetPeerInfo`, `Host`,
`GetLocalHostAddresses`, `EnumHosts`, `Connect`, `CancelAsyncOperation`,
`SendTo`, `ReturnBuffer`, `GetSendQueueInfo`, `GetPlayerContext`,
`EnumPlayersAndGroups`, `GetApplicationDesc`/`SetApplicationDesc`,
`GetConnectionInfo`, `GetPeerAddress`, `Close`, `TerminateSession`,
`DestroyPeer`.
Nachrichten: CREATE_PLAYER (beim Host für sich selbst noch in `Host()`),
DESTROY_PLAYER, RECEIVE, SEND_COMPLETE, CONNECT_COMPLETE (mit
Antwortdaten), INDICATE_CONNECT, RETURN_BUFFER, ENUM_HOSTS_RESPONSE,
TERMINATE_SESSION (mit Daten), ASYNC_OP_COMPLETE.

Stubs (`DPNERR_UNSUPPORTED`, geloggt): Gruppen, `RegisterLobby`.
Nicht eingebaut: HOST_MIGRATE (geht der Host, endet die Sitzung),
Fragmentierung großer Pakete (V10: gemessen höchstens 311 Byte, größere
Datagramme zerlegt IP selbst), Hole-Punching.

Zwei Annahmen sind bewusst an einer Stelle gebündelt, damit sie nach dem
Zwei-Spieler-Mitschnitt (`directplay-capture/CAPTURE-GUIDE.md`) mit einer
Zeile korrigierbar sind: die Reihenfolge der Meldungen beim Beitritt
(`HandleInner`, Fall `IN_WELCOME`: erst CREATE_PLAYER lokal, dann die
anderen, dann CONNECT_COMPLETE) und die Loopback-Regel in `SendTo`.

## Selbsttest

```
build.bat
build-test.bat
python -m unittest tests.test_relay_dll
```

Prüft Hosten (CREATE_PLAYER noch in `Host()`), Beitritt zweier Spieler mit
Adress-Roundtrip über den URL-String, Roster, Abweisung mit Antwortdaten
und RETURN_BUFFER, einen Geister-Teilnehmer ohne Hallo, Spielerinfos,
Broadcast mit und ohne Loopback, gerichtete Sends, gemessene Rundlaufzeit,
50 garantierte Nachrichten unter 30 % Verlust, NAT-Adresswechsel,
Rauswurf mit 4 Datenbytes, späten Beitritt nach dem Rauswurf, Verlassen,
Host-Abgang und Beitritt zu einer nicht existierenden Sitzung. Dazu zwei
eigene Läufe: Relay fällt mitten in der Sitzung aus (`test_relay.exe
relaykill`) und Relay mit 100 ± 25 ms Verzögerung und 5 % Verlust
(`test_relay.exe impaired`, 300 garantierte Nachrichten in 15 s).
