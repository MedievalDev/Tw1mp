"""Binary wire protocol for the Two Worlds 1 lobby (default port 17171).

The login sequence exchanges zlib-compressed packets prefixed with a
32-bit little-endian total length. After login, commands are exchanged
as NUL-terminated text, some followed by raw or compressed binary blobs.

Protocol reverse engineering: buglord (CC0),
https://github.com/buglord/Two-Worlds-1-Misc-Projects
"""

import hashlib
import struct
import zlib

LOBBY_PORT = 17171
RECV_BUF_LEN = 4096

NUL = b'\0'
# Base value the client's serial-related identifier is XORed onto during
# the handshake; the result seeds the serial response in the welcome packet.
SERIAL_KEY_BASE = struct.pack('<II', 0xA6AE1F9B, 0x438DFF40)

_32bit = 0xFFFFFFFF
_8bit = 0xFF

_G64_BASE = bytes([
    0xD2, 0x12, 0x13, 0xD3, 0x11, 0xD1, 0xD0, 0x10, 0xF0, 0x30,
    0x31, 0xF1, 0x33, 0xF3, 0xF2, 0x32, 0x36, 0xF6, 0xF7, 0x37,
    0xF5, 0x35, 0x34, 0xF4, 0x3C, 0xFC, 0xFD, 0x3D, 0xFF, 0x3F,
    0x3E, 0xFE, 0xFA, 0x3A, 0x3B, 0xFB, 0x39, 0xF9, 0xF8, 0x38,
    0x28, 0xE8, 0xE9, 0x29, 0xEB, 0x2B, 0x2A, 0xEA, 0xEE, 0x2E,
    0x2F, 0xEF, 0x2D, 0xED, 0xEC, 0x2C, 0xE4, 0x24, 0x25, 0xE5,
    0x27, 0xE7, 0xE6, 0x26])


def _step(num):
    return (num * 0x343FD + 0x269EC3) & _32bit


def gen64(combined):
    """Generate the 64-byte serial response for the welcome packet."""
    out = bytearray(0x40)
    ebp = edi = tmp = 0
    for b in combined:
        edi += b + tmp
        tmp ^= b
        ebp += tmp
    for i in range(0x40):
        res = combined[(ebp + i) % 8]
        out[i] = res ^ _G64_BASE[(edi + i) % 0x40]
    rg = edi + ebp
    for i in range(0x40):
        rg = _step(rg)
        out[i] ^= (rg >> 0x10) & _8bit
    for i in range(0x20):
        rg = _step(rg)
        sA = (rg >> 0x10) % 0x40
        rg = _step(rg)
        sB = (rg >> 0x10) % 0x40
        (out[sA], out[sB]) = (out[sB], out[sA])
    return bytes(out)


def make_dstr(text):
    """Delphi-style 32-bit length prefixed string."""
    raw = text.encode('latin-1', errors='replace')
    return struct.pack('<I', len(raw)) + raw


def parse_dstr(data, off):
    [strlen] = struct.unpack('<I', data[off:off + 4])
    off += 4 + strlen
    text = data[off - strlen:off].decode('latin-1')
    return text, off


def compress_packet(payload):
    """Wrap payload as a length-prefixed zlib-compressed packet."""
    cdata = zlib.compress(payload)
    return struct.pack('<I', len(cdata) + 4) + cdata


def server_info_packet(servername):
    info = f'+"{servername}""TWMP2;10.0.0.5"'
    return compress_packet(struct.pack('<I', 0) + make_dstr(info))


def login_error_packet(message):
    return compress_packet(struct.pack('<I', 1) + make_dstr(message))


def guild_rank_points(seed=0):
    # Echoing fixed values back is accepted by the client; required for
    # game start to work. Exact meaning unknown.
    return (1153721648, 409151997, 1543387035, 1810309313)


def server_welcome_packet(serial, title, motd, seed=0):
    head = struct.pack('<I', 0) + bytes([0x55, 0xA6, 0xD8, 0x3B])
    tail = bytes(49)
    tail += gen64(serial)
    tail += struct.pack('<6I', 0, seed, *guild_rank_points(seed))
    return compress_packet(head + make_dstr(title) + make_dstr(motd) + tail)


def pretty_guid(guid):
    (a, b, c, d) = struct.unpack('<IHH8s', guid)
    da = ''.join('{:02x}'.format(i) for i in d[0:2])
    db = ''.join('{:02x}'.format(i) for i in d[2:])
    return '{:08x}-{:04x}-{:04x}-{}-{}'.format(a, b, c, da, db)


def guid_is_broken(guid):
    """True for the near-empty ids some installations send.

    Seen in the field: 00001600-0000-0000-0000-000000000000, i.e. sixteen
    bytes of which exactly one is non-zero. Other clients refuse to place a
    figure for such a player — the name shows up in the list and the chat
    works, but the character is never drawn.
    """
    if not guid or len(guid) != 16:
        return True
    return sum(1 for b in guid if b) < 4


def derive_guid(seed):
    """A stable, valid-looking v4 GUID derived from `seed`.

    Same seed always yields the same id, so a player keeps their identity
    across sessions, and different players never collide.
    """
    raw = bytearray(hashlib.sha256(
        ('tw1mp-guid:' + seed).encode('utf-8')).digest()[:16])
    # Make it read as a version-4 GUID in pretty_guid()'s output: the third
    # group starts with '4', the fourth with 8/9/a/b.
    raw[7] = (raw[7] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return bytes(raw)


def em(msg):
    """Encode a lobby text command, NUL-terminated."""
    return msg.encode('latin-1', errors='replace') + NUL


def read_compressed_blob(data, con):
    """Consume one zlib stream that may continue beyond `data`.

    Returns (blob_compressed_bytes, remaining_unused_data).
    """
    dcmp = zlib.decompressobj()
    dcmp.decompress(data)
    cdats = [data]
    con.settimeout(None)
    while not dcmp.eof:
        cdat = con.recv(RECV_BUF_LEN)
        if not cdat:
            raise ConnectionResetError('connection closed inside blob')
        cdats.append(cdat)
        dcmp.decompress(cdat)
    if len(dcmp.unused_data):
        cdats[-1] = cdats[-1][:-len(dcmp.unused_data)]
    return b''.join(cdats), dcmp.unused_data
