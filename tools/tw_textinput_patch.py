#!/usr/bin/env python3
"""Two Worlds 1 text-input fix (keyboard in text boxes / chat / login).

The game reads keyboard input in its text-field message pump with a
PeekMessageA call whose wMsgFilterMin/Max drop the keyboard messages on
modern Windows, so typing barely registers. This applies the known
InsideTwoWorlds fix: redirect that ONE PeekMessageA call site to a small
wrapper (placed in spare zero padding at the end of .text) that forces the
filter to WM_KEYFIRST(0x100)..WM_KEYLAST(0x109) and hWnd=NULL, then calls the
real PeekMessageA. Every other PeekMessageA call is left untouched.

Supports two builds (identified by MD5):
  * TwoWorlds.exe        MD5 FF0F7C0E6D180847006A1D25D125F8B4 (== the public
    fix's target build byte-for-byte at every patch site)
  * TwoWorlds_RADEON.exe MD5 948D259CF4C7E472E37CCBECD5358BED (offsets derived
    here: identical PeekMessageA call-site structure, own IAT + code cave)

Every edit is guarded by its expected original bytes; the script refuses to
run on anything else and writes a .bak backup first. Fully reversible (Steam
"Verify integrity of game files" also restores the original).

Usage:
    python tw_textinput_patch.py "<path to exe>"          # patch (makes .bak)
    python tw_textinput_patch.py --verify "<path to exe>" # check status only
    python tw_textinput_patch.py --restore "<path to exe>" # restore from .bak
"""

import hashlib
import os
import shutil
import struct
import sys

# The 37-byte PeekMessageA wrapper. The 4 bytes at index 26..29 are the
# absolute address of the real PeekMessageA IAT slot and differ per build.
_WRAPPER_TEMPLATE = bytes([
    0x55,                    # push ebp
    0x89, 0xE5,              # mov  ebp, esp
    0x57,                    # push edi
    0x8B, 0x45, 0x18,        # mov  eax, [ebp+0x18]  (wRemoveMsg)
    0x50,                    # push eax
    0x68, 0x09, 0x01, 0x00, 0x00,   # push 0x109  (WM_KEYLAST)
    0x68, 0x00, 0x01, 0x00, 0x00,   # push 0x100  (WM_KEYFIRST)
    0x6A, 0x00,              # push 0            (hWnd = NULL)
    0x8B, 0x45, 0x08,        # mov  eax, [ebp+0x8]   (lpMsg)
    0x50,                    # push eax
    0x8B, 0x3D, 0, 0, 0, 0,  # mov  edi, ds:[<PeekMessageA IAT>]  <- filled in
    0xFF, 0xD7,              # call edi
    0x5F,                    # pop  edi
    0x5D,                    # pop  ebp
    0xC2, 0x14, 0x00,        # ret  0x14
])
_IAT_SLOT_IN_WRAPPER = 26   # offset of the 4-byte IAT address inside the wrapper


def _wrapper(iat_va):
    w = bytearray(_WRAPPER_TEMPLATE)
    w[_IAT_SLOT_IN_WRAPPER:_IAT_SLOT_IN_WRAPPER + 4] = struct.pack('<I', iat_va)
    return bytes(w)


# Per-build patch definition. call_off/call_new redirect the text-input call
# site; slot_off holds a 4-byte pointer to the wrapper; wrap_off holds the
# wrapper code. slot_off/wrap_off must currently be zero padding.
_BUILDS = {
    'FF0F7C0E6D180847006A1D25D125F8B4': {   # TwoWorlds.exe
        'name': 'TwoWorlds.exe',
        'iat_va': 0x009753A8,
        'call_off': 0x002DFB9A, 'call_orig': b'\xA8\x53', 'call_new': b'\x18\x49',
        'slot_off': 0x00573D18, 'slot_va': 0x00974920, 'wrap_off': 0x00573D20,
    },
    '948D259CF4C7E472E37CCBECD5358BED': {   # TwoWorlds_RADEON.exe
        'name': 'TwoWorlds_RADEON.exe',
        'iat_va': 0x0096D398,
        'call_off': 0x002DFC5A, 'call_orig': b'\x98\xD3', 'call_new': b'\x40\xCA',
        'slot_off': 0x0056BE40, 'slot_va': 0x0096CA48, 'wrap_off': 0x0056BE48,
    },
}


def _md5(data):
    return hashlib.md5(data).hexdigest().upper()


def _edits(spec):
    """Return the list of (offset, expected_original, new_bytes) edits."""
    wrapper = _wrapper(spec['iat_va'])
    return [
        (spec['call_off'], spec['call_orig'], spec['call_new']),
        (spec['slot_off'], b'\x00' * 4, struct.pack('<I', spec['slot_va'])),
        (spec['wrap_off'], b'\x00' * len(wrapper), wrapper),
    ]


def _classify(data):
    """Return (spec, state) where state is 'original', 'patched', or 'unknown'."""
    md5 = _md5(data)
    spec = _BUILDS.get(md5)
    if spec:
        return spec, 'original'
    # Maybe already patched: match on the post-image at the call site.
    for s in _BUILDS.values():
        off, _orig, new = s['call_off'], s['call_orig'], s['call_new']
        if data[off:off + len(new)] == new:
            return s, 'patched'
    return None, 'unknown'


def verify(path):
    data = open(path, 'rb').read()
    spec, state = _classify(data)
    print(f'file : {path}')
    print(f'md5  : {_md5(data)}')
    if not spec:
        print('build: UNKNOWN (not a supported TwoWorlds build)')
        return 2
    print(f'build: {spec["name"]}')
    if state == 'patched':
        print('state: ALREADY PATCHED (text-input fix present)')
    else:
        print('state: original (unpatched)')
    return 0


def patch(path):
    data = bytearray(open(path, 'rb').read())
    spec, state = _classify(data)
    if not spec:
        print(f'Refusing: {path} is not a recognised TwoWorlds build '
              f'(md5 {_md5(data)}).')
        return 2
    if state == 'patched':
        print(f'{spec["name"]} is already patched. Nothing to do.')
        return 0
    edits = _edits(spec)
    for off, expect, _new in edits:      # validate every pre-image first
        if bytes(data[off:off + len(expect)]) != expect:
            print(f'Refusing: unexpected bytes at 0x{off:X} '
                  f'(build differs from expected). No changes made.')
            return 3
    bak = path + '.bak'
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f'Backup written: {bak}')
    for off, _expect, new in edits:
        data[off:off + len(new)] = new
    with open(path, 'wb') as f:
        f.write(data)
    # post-verify
    check = open(path, 'rb').read()
    for off, _expect, new in edits:
        if check[off:off + len(new)] != new:
            print('ERROR: post-write verification failed!')
            return 4
    print(f'Patched {spec["name"]} (md5 now {_md5(check)}). '
          f'Text input in the game should work now.')
    return 0


def restore(path):
    bak = path + '.bak'
    if not os.path.exists(bak):
        print(f'No backup found at {bak}.')
        return 2
    shutil.copy2(bak, path)
    print(f'Restored {path} from {bak}.')
    return 0


def main(argv):
    if len(argv) == 3 and argv[1] == '--verify':
        return verify(argv[2])
    if len(argv) == 3 and argv[1] == '--restore':
        return restore(argv[2])
    if len(argv) == 2:
        return patch(argv[1])
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
