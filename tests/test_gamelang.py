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
