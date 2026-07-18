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
* Desktop UI (Windows): start/stop, live log, connected players, minimise to
  the notification area, and character management — download an account from a
  live server (e.g. the official one) and import it locally
* Test suite with a simulated game client covering the whole protocol

## Quickstart

Requires Python 3.10+ (no packages to install):

```sh
git clone https://github.com/MedievalDev/Tw1mp.git
cd Tw1mp
python3 TW1MP-Server.py
```

On Windows you can instead double-click **`TW1MP-UI.pyw`** for the desktop
front-end (`--tray` starts it minimised, `--no-start` opens it without
launching the server).

On first start a `Config.ini` is created next to the server; edit it and
restart to change the server name, MOTD, ports and other settings.
`ServerData.db` (accounts) and `PlayerData/` (savegames) are created in the
same directory — back these up.

Run the tests with:

```sh
python3 -m unittest discover -s tests
```

## Connecting the game / Verbindung mit dem Spiel

**English:** No `hosts` file editing is needed. The game reads its server list
from the registry, so a community server can simply be added as another entry.
On the client, under
`HKEY_CURRENT_USER\SOFTWARE\Reality Pump\TwoWorlds\Network`, prepend your
server to `EarthNet_ServersAddresses`, which is a string of `"name""host"`
pairs:

```
"My Server""203.0.113.10""WarNet Europe""warnet.2-worlds.com"
```

The server then appears by name in the game's server selection dialog.
`EarthNet_ServerPort` (default 17171) is the port the client dials; forward it
on the server. Values are per-user (HKCU), so no administrator rights are
needed — back up the key before editing.

The lobby only handles matchmaking, chat and player data. The actual game
session runs peer-to-peer over **DirectPlay** between the players, and the
host advertises its *private* LAN address (the official servers used a NAT
resolver, which this server does not provide). So: same LAN works out of the
box, over the internet everyone needs a VPN (ZeroTier, Tailscale, Hamachi) or
port forwarding on the hosting player's side.

**Note:** Hosting a game requires the Windows **DirectPlay** legacy component.
Without it the client crashes in `dpnet.dll` when starting a session — against
this server *and* the official ones. Enable it in an elevated PowerShell with
`Enable-WindowsOptionalFeature -Online -FeatureName DirectPlay -All`, then
reboot.

**Deutsch:** Die `hosts`-Datei wird nicht gebraucht. Das Spiel liest seine
Serverliste aus der Registry: Unter
`HKEY_CURRENT_USER\SOFTWARE\Reality Pump\TwoWorlds\Network` einen Eintrag vorn
an `EarthNet_ServersAddresses` anhängen (Format `"Name""Host"`, siehe oben) —
der Server erscheint dann im Auswahldialog des Spiels. Port aus
`EarthNet_ServerPort` (Standard 17171) am Server freigeben. Kein Admin nötig,
da HKCU; vorher sichern.

Die eigentliche Spielsitzung läuft Peer-to-Peer über **DirectPlay**, wobei der
Host seine *private* LAN-Adresse bewirbt. Im selben LAN funktioniert das
direkt, über Internet braucht es ein VPN (ZeroTier, Tailscale, Hamachi) oder
Portfreigaben beim hostenden Spieler. Zum Hosten muss außerdem die
Windows-Komponente **DirectPlay** aktiviert sein, sonst stürzt der Client beim
Spielstart in `dpnet.dll` ab — auch auf den offiziellen Servern.

## Bringing a character over from an official server

Multiplayer characters live on the server, not in the local save folder, so
moving one across means downloading it from the old server and importing it
here. In `TW1MP-UI.pyw` this is the *Characters* tab: pick the source server
and profile, press **Download and import**. It reuses the serial key and the
login the game stored on this machine, so it only works where the game is
installed, and the account must already exist locally (log in once). The
character it replaces is kept under `PlayerData/backup/`.

The same works headless:

```python
from tw1mp.config import Config
from tw1mp.database import Database
from tw1mp import savegame

name, blob = savegame.fetch_from_registry(server='warnet.2-worlds.com')
db = Database(Config(root='.'))
savegame.import_playerdata(db, name, blob)
```

Reading the character is a normal `/getplayerdata` login, so it neither
changes nor removes anything on the source server.

## Verified against the real game client

Tested with Two Worlds 1.7 (Steam "Epic Edition"): connecting, auto-login and
registration, MOTD, the town list, entering towns, chat, profile data,
character up/download and guild rank points all work. Two commands the real
client sends are documented nowhere and are ignored (as the reference server
did): `/ladder` and `/guildsladder`. Starting a hosted game could not be
verified because the client crashes in `dpnet.dll` on this machine — identical
against the official servers, see the DirectPlay note above.

## What was finished compared to TW1CS 0.2.0

* Implemented missing commands: `/whois`, `/update`, `/joinchatchannel`,
  `/leavechatchannel`, `/nick` (graceful response) — while keeping the
  reference server's silent declines (e.g. joining a full game) byte-for-byte
  so the real client sees nothing it hasn't seen before
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
* Hardening: size caps on pre-login packets and command blobs, bounded
  decompression, malformed `Config.ini` values fall back to defaults instead
  of aborting startup, login race fixed (no double sessions per account),
  keepalive `/nop` on by default (like the original official server) so dead
  connections get cleaned up
* Restructured into a package with logging, CLI (`--root`, `--port`,
  `--log`), and a test suite (35 tests) with a protocol-level fake client

## License

Like the upstream project, this code is dedicated to the public domain under
[CC0 1.0](LICENSE). Two Worlds is a trademark of its respective owners; this
is an unaffiliated community preservation project.
