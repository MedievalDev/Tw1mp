"""Admin bot - a lobby client that keeps the towns populated.

The bot speaks the ordinary wire protocol over a local socket, so the server
treats it exactly like any other player: it appears in the town, in the
player list and in chat, with no special cases in the server itself.

Two things are read straight from the CoreServer it runs beside instead of
off the wire - where the players currently are, and who just arrived -
because parsing the channel enumeration would add a lot of protocol handling
for information the process already has.

It follows the players from town to town, greets arrivals and answers a few
chat commands. It is deliberately excluded from the "player is alone" rule
that decides whether a modified character may be served (commands.py), so
running a bot does not silently disable that feature.
"""

import collections
import logging
import re
import socket
import struct
import threading
import time
import zlib

from .protocol import SERIAL_KEY_BASE, make_dstr, parse_dstr

log = logging.getLogger('tw1mp.bot')

# The 8 key bytes the bot sends in its handshake. They cannot collide with a
# real CD key, so serial binding and the ban list treat the bot as its own
# identity.
_SERIAL = b'\xB0\x77\xB0\x77\xAD\x81\x00\x01'
_GUID = b'\xB0\x77' * 8
# What the server derives from those bytes and stores on the account - the
# bot has to register under that value, not under the raw handshake bytes.
_ACCOUNT_SERIAL = bytes(base ^ key for base, key
                        in zip(SERIAL_KEY_BASE, _SERIAL))

_RECONNECT_S = 10.0     # wait before retrying a dropped connection
_TICK_S = 1.0           # socket read timeout == housekeeping interval
_NOP_TICKS = 20         # keepalive interval, in ticks
_JOIN_COOLDOWN_S = 5.0  # minimum gap between channel-join attempts
_MAX_BUF = 1 << 20      # drop unparsed binary (herodata) rather than grow
_FALLBACK_POS = '1000#2000'   # used until a real position has been captured

_RE_SEND = re.compile(r'^/send\s+"([^"]*)"\s+"(.*)"$')

# Marco's tuned set.txt - noticeably more view distance and detail than the
# stock settings. Paste into <game>\set.txt (the game reads it on start).
_VIEW_SETTINGS = [
    'Engine.FarPlane 2500        (Sichtweite, Standard ist deutlich kleiner)',
    'Engine.DLandFarClipp 7000   + DLandFarClippOBJ 7000  (Fernland + Objekte)',
    'Engine.LOD0 3200 / LOD1 6400 / LODblend 1066         (Detailstufen)',
    'Engine.AlphaFadeNear 1500 / AlphaFadeFar 3000        (Ausblenden)',
    'Engine.GrassDisp 28 / GrassQ 0.4                     (Gras)',
]

# Bot chat commands: name -> callable(bot) -> list[str]
_COMMANDS = {}


def _command(*names):
    def deco(fn):
        for n in names:
            _COMMANDS[n] = fn
        return fn
    return deco


@_command('!help', '!commands?', '!hilfe')
def _cmd_help(bot):
    return ['Befehle: !help !players !uptime !server !web !discord '
            '!commands !settings',
            f'Mehr Infos: {bot.cfg.bot_website}']


@_command('!players', '!online', '!who')
def _cmd_players(bot):
    names = sorted(bot._other_players())
    if not names:
        return ['Gerade ist sonst niemand online.']
    return [f'Online ({len(names)}): ' + ', '.join(names)]


@_command('!uptime')
def _cmd_uptime(bot):
    return ['Server laeuft seit ' + bot._uptime()]


@_command('!server', '!info')
def _cmd_server(bot):
    n = len(bot._other_players())
    return [f'{bot.cfg.title} - {n} Spieler online, laeuft seit {bot._uptime()}',
            f'Website: {bot.cfg.bot_website}']


@_command('!web', '!website', '!seite', '!homepage')
def _cmd_web(bot):
    return [f'Website & Downloads: {bot.cfg.bot_website}',
            'Dort gibt es Anleitungen, Mods und die Server-Infos.']


@_command('!discord')
def _cmd_discord(bot):
    if bot.cfg.bot_discord:
        return [f'Discord: {bot.cfg.bot_discord}']
    return ['Discord ist noch nicht eingerichtet - '
            f'aktuelle Infos gibt es auf {bot.cfg.bot_website}']


@_command('!commands', '!konsole', '!console', '!cmd')
def _cmd_gamecommands(bot):
    return ['Ingame-Konsole: mit ~ oeffnen. buglord hat alle 1086 Befehle '
            'aus der EXE extrahiert:',
            'github.com/buglord/Two-Worlds-1-Misc-Projects '
            '-> Commands/TwoWorldsCommands.txt',
            f'Kurzfassung & Beispiele: {bot.cfg.bot_website}']


@_command('!settings', '!setup', '!sicht', '!view', '!grafik')
def _cmd_settings(bot):
    return (['Mehr Sichtweite & Details - in <Spielordner>\\set.txt eintragen:']
            + _VIEW_SETTINGS
            + [f'Komplette Datei zum Kopieren: {bot.cfg.bot_website}'])


class AdminBot:
    def __init__(self, server):
        self.server = server
        self.cfg = server.config
        self.name = self.cfg.bot_name
        self._stop = threading.Event()
        self._thread = None
        self.sock = None
        self.buf = b''
        self.known = set()      # players already greeted in the current town
        self._last_join = 0.0

    # -- lifecycle ------------------------------------------------------

    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='tw1mp-bot')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._close_socket()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None

    def _close_socket(self):
        sock, self.sock = self.sock, None
        if not sock:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _run(self):
        self._ensure_account()
        while not self._stop.is_set():
            try:
                self._session()
            except Exception:
                if not self._stop.is_set():
                    log.warning('Admin bot lost its connection, retrying in '
                                '%.0fs', _RECONNECT_S, exc_info=True)
            finally:
                self._close_socket()
            self._stop.wait(_RECONNECT_S)

    def _ensure_account(self):
        """Make sure the bot's own account exists, whatever the login policy."""
        try:
            db = self.server.db
            if any(name == self.name for name, _ in db.list_users()):
                return
            db.register(self.name, _ACCOUNT_SERIAL, self.cfg.bot_password,
                        force=True)
            log.info('Created admin-bot account %r', self.name)
        except Exception:
            log.exception('Could not prepare the admin-bot account')

    # -- connection -----------------------------------------------------

    def _session(self):
        host = self.cfg.bind or '127.0.0.1'
        self.sock = socket.create_connection((host, self.cfg.port), timeout=10)
        self.buf = b''
        self._handshake()
        self._login()
        self.known = set()
        log.info('Admin bot %r is online', self.name)
        self._lobby_loop()

    def _send_packet(self, payload):
        cdata = zlib.compress(payload)
        self.sock.sendall(struct.pack('<I', len(cdata) + 4) + cdata)

    def _recv_exact(self, count):
        while len(self.buf) < count:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionResetError('server closed the connection')
            self.buf += chunk
        out, self.buf = self.buf[:count], self.buf[count:]
        return out

    def _recv_packet(self):
        [length] = struct.unpack('<I', self._recv_exact(4))
        return zlib.decompress(self._recv_exact(length - 4))

    def _handshake(self):
        self._send_packet(bytes(16) + make_dstr('ENG') + bytes(8) + _SERIAL)
        self._recv_packet()                      # server info

    def _login(self):
        self._send_packet(make_dstr(self.name) + make_dstr(self.cfg.bot_password)
                          + _GUID + struct.pack('<I', 0))
        res = self._recv_packet()
        [err] = struct.unpack('<I', res[0:4])
        if err:
            message, _ = parse_dstr(res, 4)
            raise RuntimeError(f'admin bot login refused: {message}')

    def _cmd(self, text):
        self.sock.sendall(text.encode('latin-1', 'replace') + b'\0')

    # -- main loop ------------------------------------------------------

    def _lobby_loop(self):
        self.sock.settimeout(_TICK_S)
        ticks = 0
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionResetError('server closed the connection')
                self.buf += chunk
            except socket.timeout:
                pass
            self._consume()
            self._follow_players()
            self._greet_arrivals()
            ticks += 1
            if ticks % _NOP_TICKS == 0:
                self._cmd('/nop')

    def _consume(self):
        """Pull chat lines out of the stream, ignoring everything else.

        The stream also carries binary payloads (other players' herodata),
        which we never asked to interpret; splitting on NUL and only acting
        on well-formed /send lines is enough and cannot desynchronise us.
        """
        while b'\0' in self.buf:
            line, self.buf = self.buf.split(b'\0', 1)
            text = line.decode('latin-1', 'replace')
            if text.startswith('/send "'):
                self._on_chat(text)
        if len(self.buf) > _MAX_BUF:
            self.buf = b''

    def _on_chat(self, text):
        match = _RE_SEND.match(text.strip())
        if not match:
            return
        sender, message = match.group(1), match.group(2)
        if sender == self.name:
            return                                   # our own line, echoed
        reply = self._answer(message.strip())
        if reply:
            self._say_lines(reply)

    def _answer(self, message):
        """Return a list of chat lines, or None to stay silent."""
        if not message.startswith('!'):
            return None
        command = message.split()[0].lower()
        handler = _COMMANDS.get(command)
        if not handler:
            return None
        return handler(self)

    def _say(self, text):
        """Send one chat line (quotes stripped - they delimit the command)."""
        self._cmd('/send "{}"'.format(str(text).replace('"', "'")))

    def _say_lines(self, lines):
        for line in lines:
            if line:
                self._say(line)

    def _uptime(self):
        total = int(time.time() - self.server.startTime)
        return f'{total // 3600}h {(total // 60) % 60:02d}m'

    # -- presence -------------------------------------------------------

    def _players(self):
        """The real players online (the bot itself excluded), name -> info."""
        try:
            return {name: info
                    for name, info in self.server.debug_dict_players().items()
                    if name != self.name}
        except Exception:
            return {}

    def _other_players(self):
        return list(self._players())

    def _greet_arrivals(self):
        """Greet the players sharing the bot's town.

        Chat is per channel, so this is scoped to the bot's own town -
        greeting someone elsewhere would be shouted into an empty room. That
        also covers the case where the bot follows a player into a town:
        they are new *to the bot* and get greeted on arrival. Dropping out of
        the town clears the flag, so returning later is greeted again.
        """
        channel = self._current_channel()
        if not channel:
            return
        here = {name for name, info in self._players().items()
                if info.get('town') == channel}
        for name in sorted(here - self.known):
            self._say_lines([
                f'Willkommen, {name}! Tippe !help fuer alle Befehle.',
                f'Infos, Mods & Anleitungen: {self.cfg.bot_website}',
            ])
        self.known = here

    def _current_channel(self):
        """Which town the server thinks the bot is in, or None."""
        try:
            con = self.server.state.activeUsers.get(self.name)
            if con and con.user and con.user.gamechannel:
                return con.user.gamechannel.name
        except Exception:
            pass
        return None

    def _target_channel(self):
        """The town with the most players, else the first configured one."""
        try:
            players = self.server.debug_dict_players()
        except Exception:
            return None
        towns = collections.Counter(
            info.get('town') for name, info in players.items()
            if name != self.name and info.get('town'))
        if towns:
            return towns.most_common(1)[0][0]
        maps = self.cfg.maps
        return f'{maps[0]}#translate{maps[0]}_Channel_01' if maps else None

    def _follow_players(self):
        target = self._target_channel()
        if not target or target == self._current_channel():
            return
        now = time.monotonic()
        if now - self._last_join < _JOIN_COOLDOWN_S:
            return       # a join is still in flight, or the last one failed
        self._last_join = now
        blob, posdata = self.server.load_herodata_sample()
        self._cmd('/leavegamechannel "1"')
        self._cmd(f'/requestjoingamechannel "{target}"')
        self._cmd(f'/joingamechannel "{target}" "{posdata or _FALLBACK_POS}"')
        # Without herodata the server sends no $gamechanneluser for us and we
        # are invisible to everyone (chat still works). Replay a captured blob.
        self._send_herodata(blob)
        log.info('Admin bot moving to %s%s', target,
                 '' if blob else ' (no herodata sample yet - invisible)')

    def _send_herodata(self, blob):
        """Announce our character appearance so other players can see us."""
        if not blob:
            return
        header = f'/setuserherodata "{self.name}" "{len(blob)}"'.encode(
            'latin-1', 'replace') + b'\0'
        self.sock.sendall(header + blob)
