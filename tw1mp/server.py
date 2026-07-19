"""TCP lobby server core."""

import logging
import signal
import socketserver
import struct
import threading
import time
import traceback
import zlib
from queue import SimpleQueue

from . import __version__, database, protocol
from .commands import CommandParser
from .lobby import GameState, User
from .protocol import (RECV_BUF_LEN, em, parse_dstr, read_compressed_blob,
                       server_info_packet, server_welcome_packet,
                       login_error_packet)

log = logging.getLogger('tw1mp.server')

_SHORT_TIMEOUT = 0.1
# Sanity caps for client-supplied sizes (real packets are far smaller).
_MAX_LOGIN_PACKET = 0x10000       # 64 KiB compressed login packet
_MAX_LOGIN_PLAIN = 0x100000       # 1 MiB decompressed login payload
_MAX_BLOB = 0x1000000             # 16 MiB command blob (herodata/playerdata)

_LOGIN_ERRORS = {
    database.ERR_BAD_CREDENTIALS: 'Wrong username or password',
    database.ERR_ALREADY_ONLINE: 'This account is already logged in',
    database.ERR_SHORT_PASSWORD: 'Password is too short',
    database.ERR_NO_USERNAME: 'Please log in or create an account',
    database.ERR_USER_EXISTS: 'This username is already taken',
    database.ERR_SERIAL_IN_USE: 'This serial key already has an account',
    database.ERR_BANNED: 'You are banned from this server',
    database.ERR_REGISTRATION_CLOSED: 'Registration is closed on this server',
}


class MessageDistributor:
    """Fans broadcast messages out into per-connection send queues."""
    _ENDITEM = ['STOP']

    def __init__(self, server):
        self._cQueue = SimpleQueue()
        self.server = server

    def serve_forever(self):
        while True:
            command = self._cQueue.get()
            if command == self._ENDITEM:
                break
            try:
                targets = command.get('target', [])
                msg = command.get('message')
                if msg and targets:
                    if msg != b'/nop\x00' and log.isEnabledFor(logging.DEBUG):
                        log.debug('TX* -> %s: %r',
                                  [c.user.name if c.user else '?'
                                   for c in targets], msg)
                    for con in targets:
                        con._sQueue.put(msg)
            except Exception:
                log.exception('Message distribution failed')

    def add(self, props):
        self._cQueue.put(props)

    def end(self):
        self.add(self._ENDITEM)


class ConnectionHandler(socketserver.BaseRequestHandler):
    # request: client socket, client_address, server: CoreServer

    def setup(self):
        self._sQueue = SimpleQueue()
        self.user = None
        self.guid = None
        self.data = b''
        self._reserved = None
        self._kick = False  # set by CoreServer.kick() to drop this session
        self.use_modded = False  # this session runs on a modified character
        self.SK = bytearray(protocol.SERIAL_KEY_BASE)

    def _reserveName(self, username):
        """Claim the username in activeUsers before the (slow) credential
        check, so two concurrent logins can't both pass the online check."""
        state = self.server.state
        with state.lock:
            if username in state.activeUsers:
                return False
            state.activeUsers[username] = self
            self._reserved = username
            return True

    def _releaseName(self):
        state = self.server.state
        with state.lock:
            if self._reserved and \
                    state.activeUsers.get(self._reserved) is self:
                del state.activeUsers[self._reserved]
            self._reserved = None

    def _recv(self, buflen=RECV_BUF_LEN):
        chunk = self.request.recv(buflen)
        if not chunk:
            raise ConnectionResetError('client disconnected')
        return chunk

    def readBlob(self, size):
        """Read `size` raw bytes following a command."""
        size = int(size)
        if size < 0 or size > _MAX_BLOB:
            raise ConnectionResetError(f'implausible blob size {size}')
        self.request.settimeout(None)
        while len(self.data) < size:
            self.data += self._recv()
        blbuf = self.data[0:size]
        self.data = self.data[size:]
        return blbuf

    def handle(self):
        try:
            self._loginHandle()
            self._lobbyHandle()
        except (ConnectionResetError, TimeoutError, BrokenPipeError):
            pass  # expected forms of disconnection
        except Exception:
            log.error('Connection handler crashed (user: %s)\n%s',
                      self.user.name if self.user else '<not logged in>',
                      traceback.format_exc())

    # -- login phase ----------------------------------------------------

    def _readPacket(self):
        while len(self.data) < 4:
            self.data += self._recv()
        pack_len = struct.unpack('<I', self.data[0:4])[0]
        if pack_len < 8 or pack_len > _MAX_LOGIN_PACKET:
            raise ConnectionResetError(
                f'implausible login packet length {pack_len}')
        while len(self.data) < pack_len:
            self.data += self._recv()
        dcmp = zlib.decompressobj()
        res = dcmp.decompress(self.data[4:pack_len], _MAX_LOGIN_PLAIN)
        if dcmp.unconsumed_tail:
            raise ConnectionResetError('login packet decompresses too large')
        self.data = self.data[pack_len:]
        return res

    def _loginHandle(self):
        log.info('Connection attempt from %s', self.client_address[0])
        # 1: client info
        res = self._readPacket()
        self.gameversion = res[0:16]
        langname, off = parse_dstr(res, 16)
        RK = res[off + 8:off + 16]
        for i in range(len(RK)):
            self.SK[i] ^= RK[i]
        self.SK = bytes(self.SK)
        # 2: server info
        self.request.sendall(server_info_packet(self.server.config.name))
        # 3..4: login / register until success
        while self.user is None:
            res = self._readPacket()
            username, off = parse_dstr(res, 0)
            password, off = parse_dstr(res, off)
            self.guid = bytes(res[off:off + 16])
            off += 16
            isreg = struct.unpack('<I', res[off:off + 4])[0]
            off += 4
            if isreg:
                email, off = parse_dstr(res, off)
                location, off = parse_dstr(res, off)
                age = res[off]
                gender = res[off + 1]
                off += 2
                description, off = parse_dstr(res, off)
                err = self._attemptRegister(username, password, email,
                                            location, age, gender, description)
            else:
                err = self._attemptLogin(username, password)
                if err == database.ERR_BAD_CREDENTIALS and \
                        self.server.config.auto_register:
                    rerr = self._attemptRegister(username, password,
                                                 '', '', 1, 0, '')
                    if rerr != database.ERR_USER_EXISTS:
                        err = rerr
                    # else: existing account, wrong password — keep the
                    # original credentials error
            if err == database.OK:
                cfg = self.server.config
                self.request.sendall(server_welcome_packet(
                    self.SK, cfg.title, cfg.motd))
            else:
                msg = _LOGIN_ERRORS.get(err, 'Login failed')
                log.info('Login failed for %r from %s: %s',
                         username, self.client_address[0], msg)
                if self.server.config.compat_login_errors:
                    # exact string the reference server was field-tested with
                    msg = 'TESTERROR'
                self.request.sendall(login_error_packet(msg))

    def _attemptLogin(self, username, password):
        if len(username) < 1:
            # Client sends an empty login when nothing is stored; the error
            # response makes it show the login/register prompt.
            return database.ERR_NO_USERNAME
        if len(password) < 1:
            return database.ERR_SHORT_PASSWORD
        if not self._reserveName(username):
            return database.ERR_ALREADY_ONLINE
        err = self.server.db.login(username, self.SK, password)
        if err == database.OK:
            self.user = User(username, self, self.server.state)
        else:
            self._releaseName()
        return err

    def _attemptRegister(self, username, password, email, location, age,
                         gender, description):
        if len(username) < 1:
            return database.ERR_NO_USERNAME
        if len(password) < 1:
            return database.ERR_SHORT_PASSWORD
        if not self._reserveName(username):
            return database.ERR_ALREADY_ONLINE
        err = self.server.db.register(username, self.SK, password, email,
                                      location, age, gender, description)
        if err == database.OK:
            self.user = User(username, self, self.server.state)
        else:
            self._releaseName()
        return err

    # -- lobby phase ----------------------------------------------------

    def _lobbyHandle(self):
        log.info('User %s logged in from %s', self.user.name,
                 self.client_address[0])
        with self.server.state.lock:
            self.server.state.activeUsers[self.user.name] = self
        while True:
            # flush pending outgoing messages (incl. any kick notice)
            while not self._sQueue.empty():
                self.request.sendall(self._sQueue.get())
            if self._kick:
                break
            # poll for incoming data
            self.request.settimeout(_SHORT_TIMEOUT)
            try:
                self.data += self._recv()
            except TimeoutError:
                if self.server._is_closing:
                    break
            self.request.settimeout(None)
            while self.data:
                try:
                    cmd_l = self.data.index(0)
                except ValueError:
                    break  # incomplete command, need more data
                cmd = self.data[0:cmd_l].decode('latin-1')
                self.data = self.data[cmd_l + 1:]
                log.debug('RX %s: %r', self.user.name, cmd)
                response = self.server.compars.parse(cmd, self)
                if response:
                    log.debug('TX %s: %r', self.user.name, response)
                    self.request.sendall(response)
                # Skip stray compressed blobs after commands we don't
                # consume (defensive; shouldn't occur in practice).
                if (len(self.data) > 2 and self.data[0] == 0x78 and
                        self.data[1] == 0x9C):
                    blob, self.data = read_compressed_blob(self.data,
                                                           self.request)
                    log.debug('Discarded stray blob after %r (%d bytes)',
                              cmd, len(blob))

    def finish(self):
        if self.user:
            log.info('User %s disconnected', self.user.name)
            with self.server.state.lock:
                self.user.disconnect(self.server)
        else:
            self._releaseName()  # in case login aborted mid-attempt
            log.info('Connection from %s closed before login',
                     self.client_address[0])

    def debug_dict(self):
        return {
            'game': self.user.game.gname if self.user.game else '',
            'town': self.user.gamechannel.name if self.user.gamechannel else '',
            'pos': self.user.posdata or '',
            'id': self.user.idnum,
            'loginTime': self.user.loginTime.isoformat(),
        }


class CoreServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    _is_closing = False

    def __init__(self, config, db=None):
        self.config = config
        log.info('TW1MP lobby server %s starting on port %s',
                 __version__, config.port)
        super().__init__((config.bind, config.port), ConnectionHandler)
        self.db = db or database.Database(config)
        self.dist = MessageDistributor(self)
        self.compars = CommandParser(self)
        self.state = GameState(self)
        self.startTime = time.time()
        self.service_tick = 0
        self.bot = None
        self.webapi = None
        if config.web_enabled:
            from .web import WebServer
            self.webapi = WebServer(self)

    def getPlayer(self, username):
        with self.state.lock:
            return self.state.activeUsers.get(username)

    def broadcast(self, text):
        """Send a system message to every logged-in player. Returns the
        number of recipients."""
        text = str(text).replace('"', "'").replace('\x00', ' ')
        with self.state.lock:
            targets = [c for c in self.state.activeUsers.values() if c.user]
        if targets:
            self.dist.add({'target': targets, 'message': em(f'/admin {text}')})
        log.info('Broadcast to %d player(s): %s', len(targets), text)
        return len(targets)

    def kick(self, username, notice='You have been kicked from the server'):
        """Drop a connected player. Returns True if they were online."""
        with self.state.lock:
            con = self.state.activeUsers.get(username)
            if not con or not con.user:
                return False
            if notice:
                con._sQueue.put(em(f'/admin {notice}'))
            con._kick = True
        log.info('Kicked %s', username)
        return True

    def debug_dict_players(self):
        with self.state.lock:
            return {name: con.debug_dict()
                    for name, con in self.state.activeUsers.items()
                    if con.user}  # skip reservations of in-flight logins

    def debug_dict_towns(self):
        with self.state.lock:
            return {name: chn.debug_dict()
                    for name, chn in self.state.gameChannels.items()}

    def debug_arr_games(self):
        with self.state.lock:
            ret = []
            for chn in self.state.gameChannels.values():
                ret.extend(chn.debug_arr_games())
            return ret

    def service_actions(self):  # called every poll interval (1s)
        self.state.updatePos()
        if self.config.send_nops and (self.service_tick % 3) == 0:
            with self.state.lock:
                targets = list(self.state.activeUsers.values())
            self.dist.add({'target': targets, 'message': em('/nop')})
        self.service_tick = (self.service_tick + 1) % (60 * 60 * 24 * 3)
        super().service_actions()

    def serve_forever(self, poll_interval=1):
        distThread = threading.Thread(target=self.dist.serve_forever,
                                      daemon=True)
        distThread.start()
        webThread = None
        if self.webapi:
            webThread = threading.Thread(target=self.webapi.serve_forever,
                                         daemon=True)
            webThread.start()
        if self.config.bot_enabled:
            from .adminbot import AdminBot
            self.bot = AdminBot(self)
            self.bot.start()
        try:
            # poll interval fixed to the position broadcast rate
            super().serve_forever(poll_interval)
        finally:
            if self.bot:
                self.bot.stop()
                self.bot = None
            self.dist.end()
            if self.webapi:
                self.webapi.shutdown()
                self.webapi.server_close()
                webThread.join()
            distThread.join()

    def handle_signal(self, timeout=2):
        def handler(signum, _):
            signame = signal.Signals(signum).name
            log.info('Received %s, closing in %ss', signame, timeout)
            self._is_closing = True
            with self.state.lock:
                targets = list(self.state.activeUsers.values())
            self.dist.add({'target': targets,
                           'message': em('/admin Server is shutting down')})
            time.sleep(timeout)
            self._BaseServer__shutdown_request = True
        return handler


def run(config):
    server = CoreServer(config)
    with server:
        signal.signal(signal.SIGINT, server.handle_signal(timeout=2))
        try:
            signal.signal(signal.SIGTERM, server.handle_signal(timeout=2))
        except (ValueError, OSError):
            pass
        server.serve_forever()
