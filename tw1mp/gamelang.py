"""Localised item names, read live from an installed Two Worlds game.

The game ships its translations as `.lan` files (`translate<KEY>` ->
localised UTF-16 text) inside `WDFiles/*.wd` archives. This module
contains a minimal read-only parser for both formats and builds one
merged translation table, so tools can show "Steinpilz" instead of
MUSHROOM_01. Everything is read from the local installation at runtime -
no game content is copied anywhere.

WD container layout follows buglord's CC0 "WD Repacker",
https://github.com/buglord/Two-Worlds-1-Misc-Projects (only the v1/0x200
variant used by Two Worlds 1 is supported here).
"""

import logging
import os
import re
import struct
import zlib

log = logging.getLogger('tw1mp.gamelang')

_REG_FILESYSTEM = r'SOFTWARE\WOW6432Node\Reality Pump\TwoWorlds\FileSystem'

# Archives that may carry .lan files, in patch order: later entries
# override earlier ones, mirroring how the game applies updates.
_LAN_ARCHIVES = ('Language.wd', 'Content01_Lan.wd', 'Content02_Lan.wd',
                 'Language16.wd', 'Update16.wd', 'LanguageGOG.wd')

_TRANSLATE = 'translate'


def find_game_dir():
    """Installation directory from the registry, or None."""
    if os.name != 'nt':
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            _REG_FILESYSTEM) as key:
            path, _ = winreg.QueryValueEx(key, 'DataPath')
        return path if path and os.path.isdir(path) else None
    except Exception:
        log.debug('Game directory not found in registry', exc_info=True)
        return None


# -- WD v1 archives -----------------------------------------------------

def _read_dstream(f):
    """Read one zlib stream from the current file position."""
    dobj = zlib.decompressobj()
    out = []
    while not dobj.eof:
        chunk = f.read(4096)
        if not chunk:
            raise ValueError('Truncated stream in WD archive')
        out.append(dobj.decompress(chunk))
    return b''.join(out)


def wd_list(path):
    """Directory of a TW1 (v1/0x200) WD archive: {filepath: entry}."""
    with open(path, 'rb') as f:
        head = f.read(2)
        if head == b'\x78\x9c':  # compressed header, the TW1 layout
            f.seek(0)
            head = _read_dstream(f)
            if head[4:6] != b'WD' or \
                    struct.unpack('<H', head[6:8])[0] != 0x200:
                raise ValueError(f'Not a TW1 WD archive: {path}')
        else:
            raise ValueError(f'Unsupported WD variant: {path}')
        # the file table is a zlib stream whose length sits in the
        # last 4 bytes of the archive
        f.seek(-4, 2)
        [table_len] = struct.unpack('<I', f.read(4))
        f.seek(-table_len, 2)
        table = _read_dstream(f)
    off = 8  # skip FILETIME
    [count] = struct.unpack_from('<H', table, off)
    off += 2
    entries = {}
    for _ in range(count):
        [namelen] = struct.unpack_from('<B', table, off)
        off += 1
        name = table[off:off + namelen].decode('latin-1')
        off += namelen
        flags, offset, clen, rlen = struct.unpack_from('<BIII', table, off)
        off += 13
        if flags & (1 << 3):    # extra string
            [xlen] = struct.unpack_from('<B', table, off)
            off += 1 + xlen
        if flags & (1 << 4):    # extra int
            off += 4
        if flags & (1 << 5):    # GUID
            off += 16
        entries[name] = {'flags': flags, 'offset': offset,
                         'clen': clen, 'rlen': rlen}
    return entries


def wd_read(path, filepath):
    """Extract one file from a WD archive, or None if absent."""
    entry = wd_list(path).get(filepath)
    if entry is None:
        return None
    with open(path, 'rb') as f:
        f.seek(entry['offset'])
        if entry['flags'] & 1:
            return _read_dstream(f)
        return f.read(entry['clen'])


# -- .lan translation files ---------------------------------------------

def parse_lan(data):
    """{key: text} from a .lan blob (LAN\\0, version, count, entries)."""
    if data[:4] != b'LAN\x00':
        raise ValueError('Not a .lan file')
    [count] = struct.unpack_from('<I', data, 8)
    off = 12
    out = {}
    for _ in range(count):
        [klen] = struct.unpack_from('<I', data, off)
        off += 4
        key = data[off:off + klen].decode('latin-1')
        off += klen
        [vlen] = struct.unpack_from('<I', data, off)
        off += 4
        out[key] = data[off:off + vlen * 2].decode('utf-16-le', 'replace')
        off += vlen * 2
    return out


# -- merged item-name table ---------------------------------------------

_cache = None


def load_translations(game_dir=None):
    """Merged {KEY: localised text} from all language archives.

    Keys are stored without the 'translate' prefix. Returns {} when the
    game is not installed. The result is cached per process.
    """
    global _cache
    if _cache is not None:
        return _cache
    game_dir = game_dir or find_game_dir()
    merged = {}
    if not game_dir:
        _cache = merged
        return merged
    wd_dir = os.path.join(game_dir, 'WDFiles')
    for archive in _LAN_ARCHIVES:
        path = os.path.join(wd_dir, archive)
        if not os.path.exists(path):
            continue
        try:
            for name, entry in wd_list(path).items():
                if not name.lower().endswith('.lan'):
                    continue
                blob = wd_read(path, name)
                for key, text in parse_lan(blob).items():
                    if key.startswith(_TRANSLATE):
                        merged[key[len(_TRANSLATE):]] = text
        except (OSError, ValueError, struct.error):
            log.warning('Skipping unreadable archive %s', path,
                        exc_info=True)
    log.info('Loaded %d translations from %s', len(merged), wd_dir)
    _cache = merged
    return merged


# The game's strings carry inline markup: colour tags (<0xFFFFFFFF>), format
# tags (<F2>, <t+>, <br>) and inline icons (<ico...,'Inter...dds'>).
_RE_MARKUP = re.compile(r'<[^>]*>')


def clean_text(text):
    """Strip the game's inline markup tags and surrounding whitespace."""
    return _RE_MARKUP.sub('', text).strip()


def item_name(item_id, game_dir=None):
    """Localised name for an item id, falling back to the id itself.

    Entries are stored as ``<colour>Name<colour>\\nDescription``, so the
    name is the first line with the markup removed.
    """
    text = load_translations(game_dir).get(item_id)
    if not text:
        return item_id
    return clean_text(text.split('\n', 1)[0]) or item_id
