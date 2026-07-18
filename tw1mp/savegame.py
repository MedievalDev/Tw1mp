"""Fetch a character from a live lobby server and import it locally.

Two Worlds stores multiplayer characters server-side, not in the local
save folder: the client uploads them with /setplayerdata and pulls them
back with /getplayerdata after login. This module speaks just enough of
the client side of the login handshake to request one, so a character
living on an official (or any other) server can be migrated into a local
TW1MP instance.

Serial-key decoding, the credential file layout and the download flow
follow buglord's "PlayerData Downloader" (CC0),
https://github.com/buglord/Two-Worlds-1-Misc-Projects
"""

import logging
import os
import re
import socket
import struct
import zlib

log = logging.getLogger('tw1mp.savegame')

DEFAULT_FORM = 'TwoWorlds.1.0'
DEFAULT_SERVER = 'warnet.2-worlds.com'
DEFAULT_PORT = 17171
DEFAULT_PROFILE = 'default'

# GUID of the stock update16.wd Parameters/TwoWorlds.par parameter file.
# The server sees it as the client's content fingerprint.
PARAM_GUID = b'q\xa8}-\x9e\x1d\xe5N\x99\xcf\x89ik\xd2$n'

_REG_NETWORK = r'SOFTWARE\Reality Pump\TwoWorlds\Network'
_REG_SERIAL = r'SOFTWARE\Reality Pump\TwoWorlds\SerialKey'
_SAVE_PATH = r'%USERPROFILE%\Saved Games\Two Worlds Saves\Players'
_USERINFO = 'UserInfo.usr'

_RE_SERVERS = re.compile(r'(?:"([^"]*)""([^"]*)")')
_RE_PDRESP = re.compile(r'/getplayerdata ".+" "(?:[^"]+)" (\d+)')

# Valid serial-key alphabet (no I, O, 0, 1 - ambiguous glyphs).
_SERIAL_LETTERS = b'23456789ABCDEFGHJKLMNPQRSTUWVXYZ'
_32BIT = 0xFFFFFFFF
_8BIT = 0xFF


class SavegameError(Exception):
    """Raised for any expected, reportable failure of a transfer."""


# -- Windows registry / local files -------------------------------------

def _open_key(path):
    import winreg  # Windows-only; callers guard with is_supported()
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)


def is_supported():
    """True if the local machine can supply serial/credentials itself."""
    return os.name == 'nt'


def read_serial_key():
    """Read and de-obfuscate the installed serial key, or None."""
    try:
        import winreg
        with _open_key(_REG_SERIAL) as key:
            raw, _ = winreg.QueryValueEx(key, 'SerialKey')
        parts = list(struct.unpack('<6I', raw))
        for i, mask in enumerate((0xFFFFEDCB, 0x1234, 0xFFFFA987,
                                  0x5678, 0xFFFFEDCB, 0x5678)):
            parts[i] ^= mask
        text = struct.pack('<6I', *parts).decode('ascii').split('\0')[0]
    except Exception:
        log.debug('No serial key in registry', exc_info=True)
        return None
    # The game stores it case-insensitively; the checksum needs upper case.
    return text.upper()


def read_servers():
    """Return [(display name, hostname), ...] from the game's server list."""
    addresses = ''
    try:
        import winreg
        with _open_key(_REG_NETWORK) as key:
            addresses, _ = winreg.QueryValueEx(key, 'EarthNet_ServersAddresses')
    except Exception:
        log.debug('No server list in registry', exc_info=True)
    found = _RE_SERVERS.findall(addresses or '')
    return found or [('WarNet Europe', DEFAULT_SERVER)]


def read_port(default=DEFAULT_PORT):
    try:
        import winreg
        with _open_key(_REG_NETWORK) as key:
            port, _ = winreg.QueryValueEx(key, 'EarthNet_ServerPort')
        return int(port)
    except Exception:
        return default


def profile_dir(profile=DEFAULT_PROFILE):
    return os.path.join(os.path.expandvars(_SAVE_PATH),
                        profile or DEFAULT_PROFILE)


def list_profiles():
    root = os.path.expandvars(_SAVE_PATH)
    try:
        return sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


def read_player_login(profile=DEFAULT_PROFILE):
    """Read (username, password) the game stored for auto-login."""
    path = os.path.join(profile_dir(profile), _USERINFO)
    try:
        with open(path, 'rb') as f:
            raw = zlib.decompress(f.read())
        [namelen] = struct.unpack('<I', raw[4:8])
        off = 8 + namelen * 2
        [ulen] = struct.unpack('<I', raw[off:off + 4])
        username = raw[off + 4:off + 4 + ulen * 2].decode('UTF-16LE')
        off += ulen * 2 + 4
        [plen] = struct.unpack('<I', raw[off:off + 4])
        password = raw[off + 4:off + 4 + plen * 2].decode('UTF-16LE')
        return username, password
    except (OSError, zlib.error, struct.error, UnicodeDecodeError):
        log.debug('No stored login in %s', path, exc_info=True)
        return None, None


# -- serial key checksum ------------------------------------------------

def _shl1(i):
    tmp = 1 << i
    return tmp & _32BIT, (tmp >> 32) & _32BIT


def process_serial_key(serial):
    """Derive the 8-byte handshake value from a XXXX-XXXX-XXXX-XXXX key."""
    if not serial:
        return None
    raw = serial.upper().encode('ascii', errors='replace')
    bits = [0] * 10
    i = 0
    for char in raw:
        if char == 0x2D:  # '-'
            if i % 4:
                return None  # separator in the wrong place
            continue
        pos = _SERIAL_LETTERS.find(char)
        if pos == -1:
            return None  # not a valid key character
        a, b = divmod(i, 8)
        bits[a] |= (pos << b) & _8BIT
        if b > 3:
            bits[(i + 5) // 8] |= (pos >> (8 - b)) & _8BIT
        i += 5
    sum_a = sum_b = 0
    z = 0
    for a in range(0x40):
        b, c = _shl1(a)
        # Positions covered by these masks hold check digits rather than
        # payload; for them the payload bit is taken from further along.
        if (b & 0xC1AC0448) == 0 and (c & 0x2080C240) == 0:
            if bits[a // 8] & (1 << (a % 8)):
                sum_a |= b
                sum_b |= c
        else:
            if bits[(z + 0x40) // 8] & ((1 << (z % 8)) & _8BIT):
                sum_a |= b
                sum_b |= c
            z += 1
    sum_a ^= 0xA6AE1F9B
    sum_b ^= 0x438DFF40
    return struct.pack('<II', sum_a, sum_b)


# -- client side of the login handshake ---------------------------------

def _client_info_packet(serial_hash):
    payload = b'\xc3\x07\xc6X\x814\x15L\xa6\x00\x86}^\xc1\xa3@'
    payload += struct.pack('<I3sQ', 3, b'ENG', 0)
    payload += serial_hash
    comp = zlib.compress(payload)
    return struct.pack('<I', len(comp) + 4) + comp


def _client_login_packet(username, password, guid):
    user = username.encode('latin-1', errors='replace')
    pw = password.encode('latin-1', errors='replace')
    payload = struct.pack(f'<I{len(user)}sI{len(pw)}s',
                          len(user), user, len(pw), pw)
    payload += guid + struct.pack('<I', 0)  # 0 = login, not registration
    comp = zlib.compress(payload)
    return struct.pack('<I', len(comp) + 4) + comp


def _read_reply(sock):
    """Read one length-prefixed packet; returns (is_error, message)."""
    data = b''
    try:
        while len(data) < 4:
            chunk = sock.recv(4096)
            if not chunk:
                raise SavegameError('Server closed the connection')
            data += chunk
        [total] = struct.unpack('<I', data[0:4])
        while len(data) < total:
            chunk = sock.recv(4096)
            if not chunk:
                raise SavegameError('Server closed the connection')
            data += chunk
    except socket.timeout as exc:
        raise SavegameError('Timed out during the login handshake') from exc
    raw = zlib.decompress(data[4:total])
    [is_error] = struct.unpack('<I', raw[0:4])
    text = ''
    if len(raw) > 8:
        try:
            [tlen] = struct.unpack('<I', raw[4:8])
            text = raw[8:8 + tlen].decode('latin-1')
        except (struct.error, UnicodeDecodeError):
            pass
    return is_error, text


def _read_playerdata(sock, timeout=20.0):
    """Read command replies until the playerdata blob arrives."""
    sock.settimeout(timeout)
    buf = b''
    for _ in range(200):
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            raise SavegameError('Timed out waiting for the character data')
        if not chunk:
            raise SavegameError('Server closed the connection')
        buf += chunk
        while True:
            try:
                end = buf.index(0)
            except ValueError:
                break
            cmd = buf[0:end].decode('latin-1')
            buf = buf[end + 1:]
            match = _RE_PDRESP.match(cmd)
            if not match:
                continue  # /nop, chat, whatever else - keep looking
            size = int(match.group(1))
            if size == 0:
                return b''  # server has no character for this account
            while len(buf) < size:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    raise SavegameError('Transfer stalled mid-character')
                if not chunk:
                    raise SavegameError('Connection lost mid-character')
                buf += chunk
            return buf[0:size]
    raise SavegameError('Server never sent the character data')


def download_playerdata(server, port, username, password, serial_hash,
                        form=DEFAULT_FORM, guid=PARAM_GUID, timeout=15.0):
    """Log in to a lobby server and download one character. Returns bytes."""
    if not serial_hash:
        raise SavegameError('No usable serial key')
    if not username or not password:
        raise SavegameError('No stored username/password')
    try:
        conn = socket.create_connection((server, port), timeout=timeout)
    except OSError as exc:
        raise SavegameError(f'Cannot reach {server}:{port} ({exc})') from exc
    with conn:
        conn.sendall(_client_info_packet(serial_hash))
        is_error, msg = _read_reply(conn)
        if is_error:
            raise SavegameError(f'Server rejected the client: {msg}')
        conn.sendall(_client_login_packet(username, password, guid))
        is_error, msg = _read_reply(conn)
        if is_error:
            raise SavegameError(f'Login failed: {msg or "wrong credentials"}')
        conn.sendall(f'/getplayerdata "{username}" "{form}"\0'
                     .encode('latin-1'))
        return _read_playerdata(conn)


def fetch_from_registry(server=None, port=None, profile=DEFAULT_PROFILE,
                        form=DEFAULT_FORM):
    """Convenience wrapper: take credentials/serial from the local install.

    Returns (username, blob).
    """
    if not is_supported():
        raise SavegameError('Reading the local install requires Windows')
    serial = read_serial_key()
    if not serial:
        raise SavegameError('No serial key found in the registry')
    serial_hash = process_serial_key(serial)
    if not serial_hash:
        raise SavegameError(f'Serial key in the registry is invalid: {serial}')
    username, password = read_player_login(profile)
    if not username:
        raise SavegameError(
            f'No stored login in profile {profile!r} - log in once in game')
    if not server:
        # Skip loopback entries; those point at a local test server.
        for _name, host in read_servers():
            if host not in ('127.0.0.1', 'localhost', '::1'):
                server = host
                break
        server = server or DEFAULT_SERVER
    blob = download_playerdata(server, port or read_port(), username,
                               password, serial_hash, form=form)
    return username, blob


# -- local import -------------------------------------------------------

def import_playerdata(db, username, blob, form=DEFAULT_FORM):
    """Store a downloaded character for a local account.

    Returns the previous character bytes (b'' if there was none) so
    callers can back it up. On failure the raised SavegameError carries
    them as `.previous` — the file may already be overwritten by then.
    """
    if not blob:
        raise SavegameError('Nothing to import (empty character data)')
    previous = db.get_playerdata(username, form)
    if not db.set_playerdata(username, form, blob):
        err = SavegameError(f'No local account named {username!r}')
        err.previous = previous
        raise err
    if db.get_playerdata(username, form) != blob:
        err = SavegameError('Verification failed after writing')
        err.previous = previous
        raise err
    return previous
