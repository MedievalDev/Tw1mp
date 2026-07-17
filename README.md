# Tw1mp — Two Worlds 1 Community Multiplayer Server

A self-hostable lobby server for **Two Worlds 1** (2007) multiplayer, replacing
the discontinued official servers. Written in pure Python 3 (standard library
only, no dependencies).

This project continues the CC0-licensed reference server **TW1CS 0.2.0** and
protocol documentation by [buglord](https://github.com/buglord):

* [Two-Worlds-1-Misc-Projects](https://github.com/buglord/Two-Worlds-1-Misc-Projects)
  (contains the original `TW1CS.py` and the lobby protocol documentation)
* [Two-Worlds-1-Script-Extender](https://github.com/buglord/Two-Worlds-1-Script-Extender)
  (client-side plugin loader — not required for this server, but useful for
  client-side fixes and mods)

## Features

* Full login sequence of the original lobby protocol (port 17171): handshake,
  account registration, login, serial binding, welcome/MOTD
* Accounts stored in SQLite with salted PBKDF2 password hashes
  (drop-in compatible with an existing TW1CS `ServerData.db`)
* Lobby channels ("towns") for all four multiplayer maps, configurable
  channel count and player limits
* Chat channels (main/trade) including switching via `/joinchatchannel`
* Player position broadcasts on the town map
* Hero data exchange (`/setuserherodata`) so players see each other's heroes
* Game hosting: create, advertise, join (with password), start, leave —
  the actual gameplay session then runs peer-to-peer via DirectPlay between
  the game clients, as in the original design
* Server-side player data storage (`/getplayerdata`, `/setplayerdata`) with
  owner-only access
* `/whois` and `/update` profile commands, `/send` chat relay,
  `/gamecommandtouser` relay for modded use
* `Config.ini` (auto-generated on first start): server name, MOTD, ports,
  auto-registration, limits, keepalives
* Optional HTTP status API (JSON `/status`, debug listings, player data
  download) — disabled by default
* Logging, graceful shutdown with player notification
* Test suite with a simulated game client covering the whole protocol

## Quickstart

Requires Python 3.10+ (no packages to install):

```sh
git clone https://github.com/MedievalDev/Tw1mp.git
cd Tw1mp
python3 TW1MP-Server.py
```

On first start a `Config.ini` is created next to the server; edit it and
restart to change the server name, MOTD, ports and other settings.
`ServerData.db` (accounts) and `PlayerData/` (savegames) are created in the
same directory — back these up.

Run the tests with:

```sh
python3 -m unittest discover -s tests
```

## Connecting the game / Verbindung mit dem Spiel

**English:** The retail game connects to the official lobby hosts on TCP port
17171 (historically e.g. `hawk.2-worlds-us.com`). To play on a community
server, redirect that hostname to your server's IP in the client's
`hosts` file (`C:\Windows\System32\drivers\etc\hosts`), e.g.:

```
203.0.113.10 hawk.2-worlds-us.com
```

Forward TCP port 17171 on the server. The lobby only handles matchmaking,
chat and player data — the actual game session runs peer-to-peer over
DirectPlay between the players, so players may additionally need direct
connectivity to the game host (port forwarding on the hosting player's side,
or a VPN such as Radmin/ZeroTier for everyone).

**Deutsch:** Das Spiel verbindet sich auf TCP-Port 17171 mit dem offiziellen
Lobby-Host. Um einen Community-Server zu nutzen, leite den Hostnamen in der
`hosts`-Datei des Clients auf die IP deines Servers um (siehe Beispiel oben)
und gib Port 17171 auf dem Server frei. Die eigentliche Spielsitzung läuft
danach Peer-to-Peer über DirectPlay zwischen den Spielern — der Spiel-Host
braucht daher ebenfalls erreichbare Ports oder alle Spieler nutzen ein VPN.

## What was finished compared to TW1CS 0.2.0

* Implemented missing commands: `/whois`, `/update`, `/joinchatchannel`,
  `/leavechatchannel`, `/nick` (graceful response), `/error` responses for
  full/running/password-protected games
* Proper login/registration error messages instead of `TESTERROR`
* Configuration file support (previously commented-out stubs)
* Chat channels are now real objects with names (required for `/whois` and
  chat switching)
* Fixed crashes: disconnect before login, unknown channel names in
  join/request commands, `remove()` call without argument when re-hosting a
  game, infinite loop on half-closed sockets, undefined variable in the web
  player data endpoint, broken rate-monitor update call
* Thread safety: all lobby state mutations now happen under a lock;
  network I/O is kept out of locked sections
* Latin-1 tolerant text handling (umlauts in chat no longer kill the
  connection thread)
* Restructured into a package with logging, CLI (`--root`, `--port`,
  `--log`), and a test suite (29 tests) with a protocol-level fake client

## License

Like the upstream project, this code is dedicated to the public domain under
[CC0 1.0](LICENSE). Two Worlds is a trademark of its respective owners; this
is an unaffiliated community preservation project.
