"""Tests for character transfer: serial handling, download, import."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from tw1mp import savegame
from tw1mp.config import Config
from tw1mp.database import Database
from tw1mp.server import CoreServer
from tw1_client import TestClient

FORM = savegame.DEFAULT_FORM
SERIAL = b'\x11\x22\x33\x44\x55\x66\x77\x88'


class TestSerialKey(unittest.TestCase):
    # Values cross-checked against buglord's reference implementation.
    KNOWN = {
        '2345-6789-ABCD-EFGH': 'fb97e36d79152486',
        'ZZZZ-ZZZZ-ZZZZ-ZZZZ': '64e05159bf0072bc',
        '7777-2222-9999-3333': '7e8f2ca640d89150',
        'QRST-UWVX-YZ23-4567': '6df9aed0ef418e43',
    }

    def test_known_keys(self):
        for key, expected in self.KNOWN.items():
            self.assertEqual(savegame.process_serial_key(key).hex(), expected,
                             f'mismatch for {key}')

    def test_case_insensitive(self):
        # The registry hands the key back in mixed case.
        upper = savegame.process_serial_key('QRST-UWVX-YZ23-4567')
        lower = savegame.process_serial_key('qrst-uwvx-yz23-4567')
        self.assertEqual(upper, lower)

    def test_rejects_invalid(self):
        for bad in ('', '1111-1111-1111-1111', 'IOIO-IOIO-IOIO-IOIO',
                    'ABCD-EFGH-IJKL-MNOP', 'AB-CD-EF'):
            self.assertIsNone(savegame.process_serial_key(bad),
                              f'{bad!r} should be rejected')


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Config(root=self.tmp.name))
        self.db.register('alice', SERIAL, 'pw')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_import_and_backup_previous(self):
        first, second = b'first-character', b'second-character'
        self.assertEqual(savegame.import_playerdata(self.db, 'alice', first),
                         b'')  # nothing there yet
        previous = savegame.import_playerdata(self.db, 'alice', second)
        self.assertEqual(previous, first)  # caller can back this up
        self.assertEqual(self.db.get_playerdata('alice', FORM), second)

    def test_rejects_empty(self):
        with self.assertRaises(savegame.SavegameError):
            savegame.import_playerdata(self.db, 'alice', b'')

    def test_rejects_unknown_account(self):
        with self.assertRaises(savegame.SavegameError):
            savegame.import_playerdata(self.db, 'nobody', b'data')

    def test_list_users(self):
        self.db.register('bob', b'\x01' * 8, 'pw')
        self.assertEqual([n for n, _ in self.db.list_users()],
                         ['alice', 'bob'])


class TestDownload(unittest.TestCase):
    """Downloads through a real server instance, end to end."""

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

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.db.close()
        self.tmp.cleanup()

    def _make_account(self, name='alice', password='pw'):
        cli = TestClient(self.port, serial=SERIAL)
        cli.handshake()
        err, _ = cli.login(name, password)
        self.assertEqual(err, 0)
        cli.close()

    def test_roundtrip(self):
        self._make_account()
        blob = bytes(range(256)) * 5
        self.server.db.set_playerdata('alice', FORM, blob)
        got = savegame.download_playerdata('127.0.0.1', self.port, 'alice',
                                           'pw', SERIAL)
        self.assertEqual(got, blob)

    def test_account_without_character(self):
        self._make_account('bob')
        got = savegame.download_playerdata('127.0.0.1', self.port, 'bob',
                                           'pw', SERIAL)
        self.assertEqual(got, b'')

    def test_wrong_password_reports_error(self):
        self._make_account()
        with self.assertRaises(savegame.SavegameError) as ctx:
            savegame.download_playerdata('127.0.0.1', self.port, 'alice',
                                         'wrong', SERIAL)
        self.assertIn('Login failed', str(ctx.exception))

    def test_unreachable_server_reports_error(self):
        with self.assertRaises(savegame.SavegameError) as ctx:
            savegame.download_playerdata('127.0.0.1', 1, 'alice', 'pw',
                                         SERIAL, timeout=2)
        self.assertIn('Cannot reach', str(ctx.exception))

    def test_missing_serial_rejected(self):
        with self.assertRaises(savegame.SavegameError):
            savegame.download_playerdata('127.0.0.1', self.port, 'alice',
                                         'pw', None)


if __name__ == '__main__':
    unittest.main()
