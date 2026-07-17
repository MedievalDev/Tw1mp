import os
import tempfile
import unittest

from tw1mp import database
from tw1mp.config import Config


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(root=self.tmp.name)
        self.db = database.Database(self.cfg)
        self.serial = b'\x01\x02\x03\x04\x05\x06\x07\x08'

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_register_and_login(self):
        self.assertEqual(self.db.register('alice', self.serial, 'pw',
                                          'a@example.org', 'DE', 30, 0, 'hi'),
                         database.OK)
        self.assertEqual(self.db.login('alice', self.serial, 'pw'),
                         database.OK)
        self.assertEqual(self.db.login('alice', self.serial, 'wrong'),
                         database.ERR_BAD_CREDENTIALS)
        self.assertEqual(self.db.login('nobody', self.serial, 'pw'),
                         database.ERR_BAD_CREDENTIALS)

    def test_register_conflicts(self):
        self.assertEqual(self.db.register('alice', self.serial, 'pw'),
                         database.OK)
        self.assertEqual(self.db.register('alice', b'\x09' * 8, 'pw'),
                         database.ERR_USER_EXISTS)
        self.assertEqual(self.db.register('bob', self.serial, 'pw'),
                         database.ERR_SERIAL_IN_USE)

    def test_serial_binding(self):
        self.assertEqual(self.db.register('alice', self.serial, 'pw'),
                         database.OK)
        self.assertEqual(self.db.login('alice', b'\x09' * 8, 'pw'),
                         database.ERR_BAD_CREDENTIALS)
        self.cfg.bind_serial = False
        self.assertEqual(self.db.login('alice', b'\x09' * 8, 'pw'),
                         database.OK)

    def test_userinfo(self):
        self.db.register('alice', self.serial, 'pw', 'a@example.org', 'DE',
                         30, 0, 'hello')
        (email, location, age, gender, desc) = self.db.get_userinfo('alice')
        self.assertEqual((email, location, gender, desc),
                         ('a@example.org', 'DE', 0, 'hello'))
        self.assertEqual(age, 30)
        self.assertTrue(self.db.update_userinfo('alice', 'b@example.org',
                                                'AT', 31, 1, 'servus'))
        (email, location, age, gender, desc) = self.db.get_userinfo('alice')
        self.assertEqual((email, location, age, gender, desc),
                         ('b@example.org', 'AT', 31, 1, 'servus'))
        self.assertIsNone(self.db.get_userinfo('nobody'))

    def test_playerdata_roundtrip(self):
        self.db.register('alice', self.serial, 'pw')
        self.assertEqual(self.db.get_playerdata('alice', 'TwoWorlds.1.0'), b'')
        blob = os.urandom(256)
        self.assertTrue(self.db.set_playerdata('alice', 'TwoWorlds.1.0', blob))
        self.assertEqual(self.db.get_playerdata('alice', 'TwoWorlds.1.0'), blob)
        # unknown user never touches disk
        self.assertFalse(self.db.set_playerdata('nobody', 'TwoWorlds.1.0', blob))


if __name__ == '__main__':
    unittest.main()
