"""Tests for the .lan parser and item-name resolution."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from tw1mp import gamelang


def lan_blob(entries):
    out = b'LAN\x00' + struct.pack('<II', 3, len(entries))
    for key, text in entries.items():
        raw_key = key.encode('latin-1')
        raw_val = text.encode('utf-16-le')
        out += struct.pack('<I', len(raw_key)) + raw_key
        out += struct.pack('<I', len(text)) + raw_val
    return out


class TestLanParser(unittest.TestCase):
    def test_roundtrip(self):
        entries = {'translateMUSHROOM_01': 'Steinpilz',
                   'translateLOCKPICK': 'Dietrich',
                   'other_key': 'ignored by the merger, kept by the parser'}
        parsed = gamelang.parse_lan(lan_blob(entries))
        self.assertEqual(parsed, entries)

    def test_umlauts(self):
        parsed = gamelang.parse_lan(
            lan_blob({'translateING_36': 'Viper-Giftdrüsen'}))
        self.assertEqual(parsed['translateING_36'], 'Viper-Giftdrüsen')

    def test_rejects_other_files(self):
        with self.assertRaises(ValueError):
            gamelang.parse_lan(b'NOTLAN' + b'\x00' * 16)

    def test_item_name_fallback_without_game(self):
        # With no game dir the id itself comes back.
        gamelang._cache = None
        try:
            self.assertEqual(gamelang.item_name('MUSHROOM_01', game_dir='-'),
                             'MUSHROOM_01')
        finally:
            gamelang._cache = None


class TestNameCleanup(unittest.TestCase):
    """Game strings are '<colour>Name<colour>\\nDescription' with inline
    markup; only the plain name should reach the UI."""

    def setUp(self):
        gamelang._cache = {
            'MAGIC_POISONDART': '<0xFFFFFFFF>Giftgruß<0xFFAAAAAA>\n'
                                'Verteilt Giftschaden beim getroffnen Gegner. ',
            'PLAIN': 'Steinpilz',
            'FORMATTED': '<F2>Trank<t+><br>',
            'ICONED': "<ico0x1l,192,0,255,63,'Inter01.dds'>Bogen<0xFF00FF00>",
            'ONLY_MARKUP': '<0xFFFFFFFF><F2>',
        }
        self.addCleanup(setattr, gamelang, '_cache', None)

    def test_strips_colour_tags_and_description(self):
        self.assertEqual(gamelang.item_name('MAGIC_POISONDART'), 'Giftgruß')

    def test_plain_name_untouched(self):
        self.assertEqual(gamelang.item_name('PLAIN'), 'Steinpilz')

    def test_strips_format_and_icon_tags(self):
        self.assertEqual(gamelang.item_name('FORMATTED'), 'Trank')
        self.assertEqual(gamelang.item_name('ICONED'), 'Bogen')

    def test_markup_only_falls_back_to_id(self):
        self.assertEqual(gamelang.item_name('ONLY_MARKUP'), 'ONLY_MARKUP')

    def test_unknown_id_falls_back(self):
        self.assertEqual(gamelang.item_name('NOPE'), 'NOPE')


@unittest.skipUnless(
    os.name == 'nt' and gamelang.find_game_dir(),
    'requires an installed Two Worlds game')
class TestInstalledGame(unittest.TestCase):
    def test_translations_load(self):
        gamelang._cache = None
        try:
            translations = gamelang.load_translations()
            self.assertGreater(len(translations), 1000)
            self.assertIn('MUSHROOM_01', translations)
        finally:
            gamelang._cache = None


if __name__ == '__main__':
    unittest.main()
