"""Tests for the item-quantity editor and the modified-variant serving."""

import os
import struct
import sys
import tempfile
import threading
import unittest
import zlib

sys.path.insert(0, os.path.dirname(__file__))

from tw1mp import chardata
from tw1mp.config import Config
from tw1mp.server import CoreServer
from tw1_client import TestClient

FORM = 'TwoWorlds.1.0'
SERIAL = b'\x11\x22\x33\x44\x55\x66\x77\x88'


def entry(name, qty, extra=b'\x0b\xaa\x00\x00\x17\x00\x05\x00'):
    """One inventory record in the layout observed in real characters:
    type byte, length-prefixed id, signature, u16 count, fixed fields."""
    raw = name.encode('ascii')
    return (b'\x02' + struct.pack('<I', len(raw)) + raw
            + b'\xff\xff\xff\xff' + b'\x00\x00\x00\x00'
            + struct.pack('<H', qty) + extra)


def gear(name, cls):
    """An equipment record: count 1, class field at +22 (real layout)."""
    raw = name.encode('ascii')
    fixed = (struct.pack('<H', 1)            # +0  count, always 1 for gear
             + b'\x55\xb0\x00\x00'           # +2  instance id
             + struct.pack('<HH', 26, 15)    # +6, +8
             + struct.pack('<II', 1, 54)     # +10, +14
             + struct.pack('<I', 9520)       # +18
             + struct.pack('<H', cls)        # +22 class ("Klasse")
             + struct.pack('<H', 1))         # +24
    return (b'\x01' + struct.pack('<I', len(raw)) + raw
            + b'\xff\xff\xff\xff' + b'\x00\x00\x00\x00' + fixed)


def character(*entries):
    """A minimal decompressed character wrapping the given item records."""
    return bytearray(b'PG\x00\x01' + b'\x00' * 28 + b''.join(entries)
                     + b'\x00' * 16)


class TestParser(unittest.TestCase):
    def test_finds_stacks_with_quantities(self):
        raw = character(entry('LOCKPICK', 54), entry('ING_36', 15),
                        entry('LOCKPICK', 7))
        items = chardata.parse_items(raw)
        self.assertEqual([(i.name, i.quantity) for i in items],
                         [('LOCKPICK', 54), ('ING_36', 15), ('LOCKPICK', 7)])

    def test_editability_by_prefix(self):
        raw = character(entry('ING_01', 3), gear('WP_SWORD_0', 1),
                        entry('MAGIC_PUSH', 2), gear('AR_RING_09', 1))
        by_name = {i.name: i for i in chardata.parse_items(raw)}
        self.assertTrue(by_name['ING_01'].editable)
        self.assertEqual(by_name['ING_01'].kind, 'count')
        # weapons/armour expose their class field, not the stack count
        self.assertTrue(by_name['WP_SWORD_0'].editable)
        self.assertEqual(by_name['WP_SWORD_0'].kind, 'class')
        self.assertTrue(by_name['AR_RING_09'].editable)
        self.assertFalse(by_name['MAGIC_PUSH'].editable)

    def test_gear_class_parse_and_patch(self):
        blob = zlib.compress(bytes(character(gear('WP_POLE_ARM_8', 3),
                                             entry('LOCKPICK', 54))))
        items = chardata.parse_items(chardata.decompress(blob))
        [halberd] = [i for i in items if i.name == 'WP_POLE_ARM_8']
        self.assertEqual(halberd.quantity, 3)  # the class, not the count
        patched = chardata.edit_quantities(blob, {halberd.qty_offset: 6})
        result = chardata.parse_items(chardata.decompress(patched))
        [halberd2] = [i for i in result if i.name == 'WP_POLE_ARM_8']
        self.assertEqual(halberd2.quantity, 6)
        [lock] = [i for i in result if i.name == 'LOCKPICK']
        self.assertEqual(lock.quantity, 54)  # untouched

    def test_gear_class_capped(self):
        blob = zlib.compress(bytes(character(gear('WP_SWORD_0', 1))))
        items = chardata.parse_items(chardata.decompress(blob))
        with self.assertRaises(chardata.CharDataError):
            chardata.edit_quantities(blob, {items[0].qty_offset: 100})

    def test_add_gear_clone(self):
        blob = zlib.compress(bytes(character(
            gear('AR_PLATE_HELMET', 2), entry('LOCKPICK', 54))))
        out = chardata.add_gear(blob, 'AR_PLATE_HELMET',
                                'AR_PLATE_HELMET_65', new_class=3)
        items = chardata.parse_items(chardata.decompress(out))
        by_name = {i.name: i for i in items}
        self.assertEqual(len(items), 3)
        self.assertEqual(by_name['AR_PLATE_HELMET'].quantity, 2)
        self.assertEqual(by_name['AR_PLATE_HELMET_65'].quantity, 3)
        self.assertEqual(by_name['LOCKPICK'].quantity, 54)
        # unique instance ids
        raw = chardata.decompress(out)
        ids = set()
        for it in items:
            base = it.qty_offset - (chardata._CLASS_FIELD_OFFSET
                                    if it.kind == 'class' else 0)
            ids.add(bytes(raw[base + 2:base + 6]))
        self.assertEqual(len(ids), 3)

    def test_add_gear_strips_socket_refs(self):
        # template ending in a socket reference (instance, type=2, zeros)
        socket_ref = (b'\x57\xb0\x00\x00'
                      + (2).to_bytes(4, 'little') + b'\x00' * 4)
        blob = zlib.compress(bytes(
            character(gear('WP_POLE_ARM_8', 6) + socket_ref,
                      entry('LOCKPICK', 7))))
        out = chardata.add_gear(blob, 'WP_POLE_ARM_8', 'WP_POLE_ARM_9')
        raw = chardata.decompress(out)
        items = chardata.parse_items(raw)
        [clone_idx] = [n for n, i in enumerate(items)
                       if i.name == 'WP_POLE_ARM_9']
        s, e = chardata._entry_bounds(raw, items, clone_idx)
        self.assertNotIn(socket_ref, bytes(raw[s:e]))
        # the template keeps its reference
        [tpl_idx] = [n for n, i in enumerate(items)
                     if i.name == 'WP_POLE_ARM_8']
        ts, te = chardata._entry_bounds(raw, items, tpl_idx)
        self.assertIn(socket_ref, bytes(raw[ts:te]))

    def test_add_gear_rejects_duplicates_and_unknown_template(self):
        blob = zlib.compress(bytes(character(gear('AR_PLATE_HELMET', 1))))
        with self.assertRaises(chardata.CharDataError):
            chardata.add_gear(blob, 'AR_PLATE_HELMET', 'AR_PLATE_HELMET')
        with self.assertRaises(chardata.CharDataError):
            chardata.add_gear(blob, 'AR_PLATE_BOOTS', 'AR_PLATE_BOOTS_65')
        with self.assertRaises(chardata.CharDataError):
            chardata.add_gear(blob, 'AR_PLATE_HELMET', 'ING_01')

    def test_ignores_signature_without_string(self):
        raw = bytearray(b'\x00' * 8 + b'\xff\xff\xff\xff\x00\x00\x00\x00'
                        + b'\x63\x00' + b'\x00' * 8)
        self.assertEqual(chardata.parse_items(raw), [])

    def test_patch_roundtrip(self):
        blob = zlib.compress(bytes(character(entry('LOCKPICK', 54),
                                             entry('ING_36', 15))))
        items = chardata.parse_items(chardata.decompress(blob))
        patched = chardata.edit_quantities(
            blob, {items[0].qty_offset: 999, items[1].qty_offset: 0})
        result = chardata.parse_items(chardata.decompress(patched))
        self.assertEqual([(i.name, i.quantity) for i in result],
                         [('LOCKPICK', 999), ('ING_36', 0)])
        # nothing but the two quantity fields changed
        before = chardata.decompress(blob)
        after = chardata.decompress(patched)
        diffs = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        allowed = {items[0].qty_offset, items[0].qty_offset + 1,
                   items[1].qty_offset, items[1].qty_offset + 1}
        self.assertTrue(set(diffs) <= allowed)

    def test_rejects_non_editable_change(self):
        blob = zlib.compress(bytes(character(entry('MAGIC_PUSH', 1))))
        items = chardata.parse_items(chardata.decompress(blob))
        with self.assertRaises(chardata.CharDataError):
            chardata.edit_quantities(blob, {items[0].qty_offset: 5})

    def test_rejects_out_of_range(self):
        blob = zlib.compress(bytes(character(entry('ING_01', 1))))
        items = chardata.parse_items(chardata.decompress(blob))
        with self.assertRaises(chardata.CharDataError):
            chardata.edit_quantities(blob, {items[0].qty_offset: 0x10000})

    def test_real_character_layout(self):
        """The pattern from the live character this was verified against."""
        raw = character(
            entry('LOCKPICK', 54, b'\x0b\xaa\x00\x00\x17\x00\x05\x00'),
            entry('LOCKPICK', 7, b'\x91\xaf\x00\x00\x17\x00\x04\x00'))
        items = chardata.parse_items(raw)
        self.assertEqual([i.quantity for i in items], [54, 7])


class TestModdedServing(unittest.TestCase):
    """Server behaviour: variant only in solo sessions, sticky saves."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(root=self.tmp.name)
        cfg.port = 0
        self.server = CoreServer(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.2},
                                       daemon=True)
        self.thread.start()
        self.original = b'ORIGINAL-CHARACTER'
        self.modded = b'MODDED-CHARACTER!!'

    def tearDown(self):
        self.server._is_closing = True
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.db.close()
        self.tmp.cleanup()

    def _login(self, name):
        cli = TestClient(self.port, serial=(name.encode() * 8)[:8],
                         guid=(name.encode() * 16)[:16])
        cli.handshake()
        err, _ = cli.login(name, 'pw')
        self.assertEqual(err, 0)
        return cli

    def _prepare(self, name='alice'):
        cli = self._login(name)  # creates the account
        cli.close()
        self.server.db.set_playerdata(name, FORM, self.original)
        self.server.db.set_playerdata(name, FORM, self.modded, modded=True)

    def _fetch(self, cli, name='alice'):
        cli.send_cmd(f'/getplayerdata "{name}" "{FORM}"')
        got = cli.wait_for(b'/getplayerdata', timeout=3)
        return got

    def test_solo_gets_modded(self):
        self._prepare()
        alice = self._login('alice')
        got = self._fetch(alice)
        self.assertIn(self.modded, got)
        self.assertNotIn(self.original, got)

    def test_with_second_player_gets_original(self):
        self._prepare()
        bob = self._login('bob')
        alice = self._login('alice')
        got = self._fetch(alice)
        self.assertIn(self.original, got)
        self.assertNotIn(self.modded, got)
        bob.close()

    def test_save_from_modded_session_keeps_original(self):
        self._prepare()
        alice = self._login('alice')
        self._fetch(alice)  # solo -> session runs on the variant
        update = b'MODDED-PROGRESS!!!'
        alice.send_cmd(
            f'/setplayerdata "alice" "{FORM}" "{len(update)}" "0" "1"',
            blob=update)
        self._fetch(alice)  # round-trip so the upload is processed
        self.assertEqual(self.server.db.get_playerdata('alice', FORM),
                         self.original)
        self.assertEqual(
            self.server.db.get_playerdata('alice', FORM, modded=True),
            update)

    def test_save_from_normal_session_updates_original(self):
        self._prepare()
        bob = self._login('bob')
        alice = self._login('alice')
        self._fetch(alice)  # not alone -> original session
        update = b'REAL-PROGRESS!!'
        alice.send_cmd(
            f'/setplayerdata "alice" "{FORM}" "{len(update)}" "0" "1"',
            blob=update)
        self._fetch(alice)
        self.assertEqual(self.server.db.get_playerdata('alice', FORM),
                         update)
        self.assertEqual(
            self.server.db.get_playerdata('alice', FORM, modded=True),
            self.modded)
        bob.close()

    def test_no_variant_solo_gets_original(self):
        cli = self._login('carol')
        cli.close()
        self.server.db.set_playerdata('carol', FORM, self.original)
        carol = self._login('carol')
        got = self._fetch(carol, 'carol')
        self.assertIn(self.original, got)


if __name__ == '__main__':
    unittest.main()


class TestCategory(unittest.TestCase):
    """Coarse grouping used for the item-list tabs."""

    def test_known_prefixes(self):
        self.assertEqual(chardata.category('WP_SWORD_01'), 'weapon')
        self.assertEqual(chardata.category('AR_HELM_01'), 'armour')
        self.assertEqual(chardata.category('POTION_HEALING_01'), 'potion')

    def test_damage_stones(self):
        self.assertEqual(chardata.category('ART_ADD_FIRE50'), 'stone')
        self.assertEqual(chardata.category('ART_ADD_SPIRIT20'), 'stone')

    def test_stones_are_not_armour(self):
        # 'ART_ADD_' must not be swallowed by the 'AR_' armour prefix.
        self.assertNotEqual(chardata.category('ART_ADD_FIRE50'), 'armour')

    def test_everything_else_is_other(self):
        for item in ('MAGIC_FIREBOLT', 'ING_12', 'LOCKPICK', 'TRAP_BOMB_04',
                     'MUSHROOM_01', 'QITEM_040', 'THE_TAINT'):
            self.assertEqual(chardata.category(item), 'other', item)

    def test_every_category_is_declared(self):
        for item in ('WP_A', 'AR_A', 'ART_ADD_A', 'POTION_A', 'ZZZ'):
            self.assertIn(chardata.category(item), chardata.CATEGORIES)
